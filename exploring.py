import pandas as pd
import jax
import jax.numpy as jnp
import jax.random as jrnd
import jax.nn as jnn
import einops as eo
import functools as ft

import numpy as np
import optax

import haiku as hk
import haiku.initializers as hki


# Read data in

df_train = pd.read_csv("./train.csv")

train_x = df_train.values[:, 1]
train_y = df_train.values[:, 0]

train_x = (train_x - 128.0) / 255.0
train_x = train_x.reshape((-1, 28, 28, 1))

X = jnp.array(train_x)
y = jnp.array(train_y, dtype=jnp.int32)

def get_generator_parallel(s, t, rng_key, batch_size, num_devices):
    def batch_generator():
        n = s.shape[0]
        key = rng_key
        kk = batch_size // num_devices
        while True:
            key, k1 = jrnd.split(key)
            perm = jrnd.choice(k1, n, shape=(batch_size,))
            
            yield s[perm, :, :, :].reshape(num_devices, kk, *s.shape[1:]), t[perm].reshape(num_devices, kk, *t.shape[1:])
    return batch_generator() 

device_count = len(jax.local_devices())


class SepConv2d(hk.Module):
    def __init__(self, in_channels, out_channels, kernel_size, stride=1, padding='SAME', rate=1):
        self.in_channels=in_channels
        self.out_channels = out_channels
        self.kernel_size = kernel_size
        self.stride = stride
        self.padding = padding
        self.rate = rate

    def __call__(self, x, is_training: bool=True):
        f0 = hki.VarianceScaling()
        x = hk.Conv2D(self.in_channels, self.kernel_size, self.stride, self.rate, self.padding, w_init=f0)(x)
        x = hk.BatchNorm(True, True, 0.98)(x, is_training)
        x = hk.Conv2D(self.out_channels, kernel_size=1, w_init=f0)(x)
        return x
    

class FeedForward(hk.Module):
    def __init__(self, dim, hidden_dim, dropout=0.6):
        self.dim = dim
        self.hidden_dim = hidden_dim
        self.dropout = dropout

    def __call__(self, x, is_training: bool = True):

        f0 = hki.VarianceScaling()
        x = hk.Linear(self.hidden_dim, w_init=f0)(x)
        x = jnn.gelu(x)
        if is_training:
            x = hk.dropout(hk.next_rng_key(), self.dropout, x)
        x = hk.Linear(self.dim, w_init=f0)(x)
        if is_training:
            x = hk.dropout(hk.next_rng_key(), self.dropout, x)
        return x    
    

class ConvAttention(hk.Module):
    def __init__(self, dim, img_size, heads=8, dim_head=64, kernel_size=3, q_stride=1, k_stride=1, v_stride=1, dropout=0.6, last_stage=False):
        self.last_stage = last_stage
        self.img_size = img_size
        self.inner_dim = dim_head * heads
        self.project_out = not (heads == 1 and dim_head == dim)

        self.heads = heads
        self.scale = dim_head ** -0.5
        self.dim = dim
        self.kernel_size = kernel_size
        self.q_stride = q_stride
        self.k_stride = k_stride
        self.v_stride = v_stride
        self.dropout = dropout

    def __call__(self, x, is_training: bool = True):
        b, n, _, h = *x.shape, self.heads
        if self.last_stage:
            cls_token = x[:, 0]
            x = x[:, 1:]
            cls_token = eo.rearrange(cls_token.expand_dims(1), 'b n (h d) -> b n d h', h = h)
        x = eo.rearrange(x, 'b (l w) n -> b l w n', l=self.img_size, w=self.img_size)
        q = SepConv2d(self.dim, self.inner_dim, self.kernel_size, self.q_stride, 'SAME')(x)
        q = eo.rearrange(q, 'b l w (h d) -> b (l w) d h', h = h)

        v = SepConv2d(self.dim, self.inner_dim, self.kernel_size, self.v_stride, 'SAME')(x)
        v = eo.rearrange(v, 'b l w (h d) -> b (l w) d h', h=h)

        k = SepConv2d(self.dim, self.inner_dim, self.kernel_size, self.k_stride, 'SAME')(x)
        k = eo.rearrange(k, 'b l w (h d) -> b (l w) d h', h=h)

        if self.last_stage:
            q = jnp.concatenate((cls_token, q), axis=3)
            v = jnp.concatenate((cls_token, v), axis=3)
            k = jnp.concatenate((cls_token, k), axis=3)

        dots = jnp.einsum('b i d h, b j d h -> b i j h', q, k) * self.scale

        attn = jnn.softmax(dots, axis=-1)

        out = jnp.einsum('b i j h, b j d h -> b i d h', attn, v)
        out = eo.rearrange(out, 'b n d h -> b n (h d)')
        if self.project_out:
            f0 = hki.VarianceScaling()
            out = hk.Linear(self.dim,w_init=f0)(out)
            if is_training:
                out = hk.dropout(hk.next_rng_key(), self.dropout, out)

        return out
    

class Transformer(hk.Module):
    def __init__(self, dim, img_size, depth, heads, dim_head, mlp_dim, dropout=0.6, last_stage=False):
        self.dim = dim
        self.img_size = img_size
        self.depth = depth
        self.heads = heads
        self.dim_head = dim_head
        self.mlp_dim = mlp_dim
        self.dropout = dropout
        self.last_stage = last_stage

    def __call__(self, x, is_training: bool=True):
        for _ in range(self.depth):
            y = x
            x = ConvAttention(self.dim, self.img_size, self.heads, self.dim_head, dropout=self.dropout, last_stage=self.last_stage)(x, is_training)
            x = hk.LayerNorm(-1, True, True)(x)
            x = y + x
            y = x
            x = FeedForward(self.dim, self.mlp_dim, self.dropout)(x, is_training)
            x = hk.LayerNorm(-1, True, True)(x)
            x = y + x
        return x
    

class CvT(hk.Module):
    def __init__(self, image_size, num_classes, dim=64, kernels=(7, 3, 3), strides=(2, 2, 1), heads=(1, 3, 6), depth=(1, 2, 10), dropout=0.6, emb_dropout=0.5, scale_dim=4):
        self.dim = dim
        self.kernels = kernels
        self.image_size = image_size
        self.depth = depth
        self.num_classes = num_classes
        self.strides = strides
        self.heads = heads
        self.dropout = dropout
        self.emb_dropout = emb_dropout
        self.scale_dim = scale_dim

    def __call__(self, x, is_training: bool = True):
        # Stage 1
        image_size = self.image_size
        x = hk.Conv2D(self.dim, self.kernels[0], self.strides[0], 1, 'SAME')(x)
        x = eo.rearrange(x, 'b h w c -> b (h w) c', h=self.image_size//2, w=self.image_size//2)
        x = hk.LayerNorm(-1, True, True)(x)
        x = Transformer(dim=self.dim, img_size=self.image_size//2, depth=self.depth[0], heads=self.heads[0], dim_head=self.dim, mlp_dim = self.dim * self.scale_dim, dropout=self.dropout)(x, is_training)
        x = eo.rearrange(x, 'b (h w) c -> b h w c', h = image_size // 2, w = image_size // 2)

        # Stage 2
        heads = self.heads
        scale = heads[1] / heads[0]
        dim = scale * self.dim
        x = hk.Conv2D(dim, self.kernels[1], self.strides[1], 1, 'SAME')(x)
        x = eo.rearrange(x, 'b h w c-> b (h w) c', h = image_size //4, w = image_size//4)
        x = hk.LayerNorm(-1, True, True)(x)
        x = Transformer(dim=dim, img_size=self.image_size//4, depth=self.depth[1], heads=self.heads[1], dim_head=self.dim, mlp_dim=dim * self.scale_dim, dropout=self.dropout)(x, is_training)
        x = eo.rearrange(x, 'b (h w) c -> b h w c', h = image_size // 4, w = image_size // 4)

        # Stage 3
        scale = heads[2] // heads[1]
        dim = scale * dim
        x = hk.Conv2D(dim, self.kernels[2], self.strides[2], 1, 'SAME')(x)
        x = eo.rearrange(x, 'b h w c-> b (h w) c', h = image_size//4, w = image_size//4)
        x = hk.LayerNorm(-1, True, True)(x)

        b, n, _ = x.shape
        cls_token = hk.get_parameter("cls_token", shape=(1, 1, dim), init=hki.VarianceScaling())
        cls_tokens = eo.repeat(cls_token, '() n d -> b n d', b = b)
        xs = jnp.concatenate((cls_tokens, x), axis=1)
        xs = Transformer(dim=dim, img_size=image_size//4, depth=self.depth[2], heads=self.heads[2], dim_head=self.dim, mlp_dim=dim * self.scale_dim, dropout=self.dropout, last_stage=True)(xs, is_training)
        x = xs[:, 0]

        x = hk.LayerNorm(-1, True, True)(x)
        x = hk.Linear(self.num_classes, w_init=hki.VarianceScaling())(x)
        return x


def process_epoch_gen(a, b, batch_size, num_devices):
    topo = batch_size // num_devices

    def epoch_generator(rng):
        n = a.shape[0]
        num_batches = n // batch_size
        key, rng = jrnd.split(rng)

        perm = jrnd.permutation(key, n)
        for i in range(num_batches):
            i0 = i * batch_size
            i1 = (i+1) * batch_size

            subp = perm[i0:i1]

            outx = jnp.array(a[subp], dtype=jnp.float32)
            outy = jnp.array(b[subp], dtype=jnp.int32)
            yield outx.reshape(num_devices, topo, *outx.shape[1:]), outy.reshape(num_devices, topo, *outy.shape[1:])

    return epoch_generator


batch_size = 8

process_gen = process_epoch_gen(X, y, batch_size, num_devices=device_count)

def build_forward_fn(image_size=28):
    def forward_fn(dgt: jnp.ndarray, *, is_training: bool) -> jnp.ndarray:
        return CvT(image_size=image_size, num_classes=10)(dgt, is_training=is_training)
    return forward_fn

