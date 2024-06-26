from collections import defaultdict
from functools import partial
from typing import Iterable

import equinox as eqx
import jax
import jax.numpy as jnp
import jax.random as random
import optax
from beartype import beartype
from jaxtyping import Array, Float, Int, jaxtyped
from optax.losses import softmax_cross_entropy_with_integer_labels
from tqdm import tqdm
from wandb.wandb_run import Run

from .datasets import ShakespearDataset
from .model import DecoderTransformer


def loader(dataset: Iterable, batch_size: int, n_iters: int, key: random.PRNGKey):
    """Yield batches of samples from the dataset."""
    for sk in random.split(key, n_iters):
        batch_ids = random.choice(sk, len(dataset), (batch_size,), replace=True)
        batch_samples = [dataset[id_] for id_ in batch_ids]
        batch_samples = jnp.stack(batch_samples)
        yield batch_samples


def count_params(model: eqx.Module) -> Int[Array, ""]:
    """Count the number of parameters of the given equinox module.
    Ignore the RoPE parameters as they are not learnable.
    """
    # Replace the params of the RoPE module by None to filter them out.
    model = jax.tree_util.tree_map_with_path(
        lambda p, v: None if "rope" in jax.tree_util.keystr(p) else v, model
    )
    params = eqx.filter(model, eqx.is_array)
    # jax.tree_util.tree_map_with_path(lambda p, _: print(p), params)

    n_params = jax.tree.map(lambda p: jnp.prod(jnp.array(p.shape)), params)
    n_params = jnp.array(jax.tree.leaves(n_params))
    n_params = jnp.sum(n_params)
    return n_params


@jaxtyped(typechecker=beartype)
@partial(jax.jit, static_argnums=1)
def loss_fn(
    params: eqx.Module,
    static: eqx.Module,
    tokens: Int[Array, "batch_size seq_len"],
) -> Float[Array, ""]:
    """Compute the loss for the given batch of tokens."""
    model = eqx.combine(params, static)
    x, y = tokens[:, :-1], tokens[:, 1:]
    y_logits = jax.vmap(model)(x)
    loss = softmax_cross_entropy_with_integer_labels(y_logits, y)
    return jnp.mean(loss)


@jaxtyped(typechecker=beartype)
@partial(jax.jit, static_argnums=1)
def batch_metrics(
    params: eqx.Module,
    static: eqx.Module,
    tokens: Int[Array, "batch_size seq_len"],
) -> dict[str, Float[Array, ""]]:
    """Compute the metrics for the given batch of tokens."""
    metrics = dict()
    model = eqx.combine(params, static)
    x, y = tokens[:, :-1], tokens[:, 1:]
    y_logits = jax.vmap(model)(x)

    metrics["cross-entropy"] = softmax_cross_entropy_with_integer_labels(y_logits, y)
    metrics["accuracy"] = y_logits.argmax(axis=2) == y

    metrics = jax.tree.map(jnp.mean, metrics)
    return metrics


def eval(
    model: DecoderTransformer,
    dataset: ShakespearDataset,
    batch_size: int,
    n_iters: int,
    key: random.PRNGKey,
) -> dict[str, Float[Array, ""]]:
    """Evaluate the model and averages the metrics over the number of iterations."""
    metrics = defaultdict(list)
    params, static = eqx.partition(model, eqx.is_array)

    dataloader = tqdm(
        loader(dataset, batch_size, n_iters, key),
        desc="Evaluating",
        total=n_iters,
        leave=False,
    )

    for batch in dataloader:
        metrics_ = batch_metrics(params, static, batch)
        for name, value in metrics_.items():
            metrics[name].append(value)

    metrics = {name: jnp.array(values) for name, values in metrics.items()}
    metrics = jax.tree.map(jnp.mean, metrics)
    return metrics


def train(
    model: DecoderTransformer,
    train_dataset: ShakespearDataset,
    test_dataset: ShakespearDataset,
    learning_rate: float,
    batch_size: int,
    n_training_iter: int,
    n_eval_iter: int,
    logger: Run,
    key: random.PRNGKey,
):
    """Main training loop for the model.

    ---
    Args:
        model: The model to train.
        train_dataset: The dataset to train on.
        test_dataset: The dataset to evaluate on.
        learning_rate: The learning rate of the optimizer.
        batch_size: The size of the batches.
        n_training_iter: The number of training iterations.
        n_eval_iter: The number of evaluation iterations used to estimate the metrics.
        logger: The wandb logger.
        key: The random key to use.
    """
    grad_fn = jax.grad(loss_fn)
    params, static = eqx.partition(model, eqx.is_array)
    optimizer = optax.adamw(learning_rate)
    opt_state = optimizer.init(params)

    n_params = count_params(model)
    print(f"Number of parameters: {n_params:,}")

    key, sk = random.split(key)
    dataloader = tqdm(
        loader(train_dataset, batch_size, n_training_iter, sk),
        desc="Training",
        total=n_training_iter,
    )

    for iter_id, batch in enumerate(dataloader):
        grads = grad_fn(params, static, batch)
        updates, opt_state = optimizer.update(grads, opt_state, params)
        params = optax.apply_updates(params, updates)

        if iter_id % 100 == 0:
            all_metrics = dict()
            model = eqx.combine(params, static)

            for dataset, prefix in [(train_dataset, "train"), (test_dataset, "test")]:
                key, sk = random.split(key)
                metrics = eval(model, test_dataset, batch_size, n_eval_iter, key)
                metrics = jax.tree.map(float, metrics)
                metrics = {f"{prefix}/{name}": value for name, value in metrics.items()}
                all_metrics.update(metrics)

            logger.log(all_metrics)
