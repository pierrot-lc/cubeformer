import equinox as eqx
import equinox.nn as nn
import jax
import jax.numpy as jnp
import jax.random as random
from beartype import beartype
from jaxtyping import Array, Bool, Float, Int, jaxtyped


class DecoderOnlyLayer(eqx.Module):
    mha: nn.MultiheadAttention
    ffn: nn.Sequential
    norm_1: nn.LayerNorm
    norm_2: nn.LayerNorm

    def __init__(self, d_model: int, num_heads: int, key: random.PRNGKey):
        super().__init__()
        assert d_model % num_heads == 0

        key, sk = random.split(key)
        self.mha = nn.MultiheadAttention(num_heads, d_model, key=sk)

        key, sk_1, sk_2 = random.split(key, 3)
        self.ffn = nn.Sequential(
            [
                nn.Linear(d_model, d_model * 4, use_bias=False, key=sk_1),
                nn.Lambda(jax.nn.relu),
                nn.Linear(4 * d_model, d_model, use_bias=False, key=sk_2),
            ]
        )

        self.norm_1 = nn.LayerNorm(d_model)
        self.norm_2 = nn.LayerNorm(d_model)

    @eqx.filter_jit
    @jaxtyped(typechecker=beartype)
    def __call__(
        self, x: Float[Array, "seq_len d_model"], mask: Bool[Array, "seq_len seq_len"]
    ) -> Float[Array, "seq_len d_model"]:
        x_att = self.mha(x, x, x, mask)
        x = self.norm_1(x + x_att)

        x_ffn = self.ffn(x)
        x = self.norm_2(x + x_ffn)

        return x


class DecoderOnlyTransformer(eqx.Module):
    layers: nn.Sequential[nn.DecoderOnlyLayer]
    embedding: nn.Embedding
    logits: nn.Linear

    def __init__(
        self,
        num_embeddings: int,
        d_model: int,
        num_heads: int,
        num_layers: int,
        num_logits: int,
        key: random.PRNGKey,
    ):
        super().__init__()

        key, sk = random.split(key)
        self.embedding = nn.Embedding(num_embeddings, d_model, key=sk)

        key, *subkeys = random.split(key, num_layers)
        self.layers = nn.Sequential(
            [DecoderOnlyLayer(d_model, num_heads, sk) for sk in subkeys]
        )

        self.logits = nn.Linear(d_model, num_logits, key=key)

    @eqx.filter_jit
    @jaxtyped(typechecker=beartype)
    def __call__(self, x: Int[Array, "seq_len"]) -> Float[Array, "seq_len d_model"]:
        mask = jnp.eye(x.shape[0], dtype=jnp.int)
        mask = jnp.cumsum(mask, axis=1).T
        mask = mask.astype(jnp.bool)

        x = jax.vmap(self.embedding)(x)

        for decoder_layer in self.layers:
            x = decoder_layer(x, mask)

        x = self.logits(x)
        return x