import functools

import numpy as np
import sklearn
import sklearn.metrics
import torch

import bb_network_decomposition.constants
import bb_network_decomposition.stats


def get_output(X, Y, intercepts, coeffs):
    for i in range(len(coeffs)):
        X = torch.mm(X, coeffs[i]) + intercepts[i]
        if i < len(coeffs) - 1:
            X = torch.tanh(X)

    # null model
    if len(coeffs) == 0:
        # workaround for RuntimeError: unsupported operation: more than one element of
        # the written-to tensor refers to a single memory location. Please clone() the
        # tensor before performing the operation.
        X = intercepts[-1] + Y * 0

    return X


def evaluate_binomial(X, Y, total_counts, scale, intercepts, coeffs):
    logits = get_output(X, Y, intercepts, coeffs)

    assert logits.shape[-1] == 1
    logits = logits[:, 0]

    probs = torch.sigmoid(logits)
    binomial = torch.distributions.binomial.Binomial(
        logits=logits, total_count=total_counts
    )
    log_probs = binomial.log_prob(Y[:, 0])

    return log_probs.cpu(), probs.detach().cpu().numpy()


def evaluate_normal(X, Y, total_counts, scale, intercepts, coeffs, eps=1e-3):
    means = get_output(X, Y, intercepts, coeffs)

    normal = torch.distributions.normal.Normal(
        means, torch.nn.functional.softplus(scale) + eps
    )
    log_probs = normal.log_prob(Y)

    return log_probs.cpu(), means.detach().cpu().numpy()


def evaluate_multinomial(X, Y, total_counts, scale, intercepts, coeffs):
    logits = get_output(X, Y, intercepts, coeffs)

    probs = torch.nn.functional.softmax(logits, dim=-1)
    multinomial = torch.distributions.multinomial.Multinomial(logits=logits)
    log_probs = multinomial.log_prob(Y)

    return log_probs.cpu(), probs.detach().cpu().numpy()


def get_fitted_model(
    X,
    Y,
    total_counts,
    evaluation_fn,
    null=False,
    nonlinear=False,
    num_steps=10,
    hidden_size=8,
):
    def _get_weight(shape):
        return torch.nn.Parameter(
            torch.nn.init.orthogonal_(torch.randn(shape).to(device))
        )

    def _get_intercept(shape):
        return torch.nn.Parameter(torch.zeros(shape).to(device))

    device = X.device

    intercepts = []
    coeffs = []
    if not null:
        if nonlinear:
            intercepts += [_get_intercept((hidden_size,))]
            coeffs += [
                _get_weight((X.shape[-1], hidden_size)),
                _get_weight((hidden_size, Y.shape[-1])),
            ]
        else:
            coeffs += [_get_weight((X.shape[-1], Y.shape[-1]))]

    intercepts.append(_get_intercept((Y.shape[-1],)))

    params = coeffs + intercepts

    scale = None
    if evaluation_fn == evaluate_normal:
        assert total_counts is None
        scale = torch.nn.Parameter(torch.zeros((1,)).to(device))
        params.append(scale)

    optimizer = torch.optim.LBFGS(params, lr=0.1)

    def closure():
        optimizer.zero_grad()

        log_probs, _ = evaluation_fn(X, Y, total_counts, scale, intercepts, coeffs)
        nll = -log_probs.sum()

        nll.backward()
        return nll

    for _ in range(num_steps):
        optimizer.step(closure)

    evaluate = functools.partial(
        evaluation_fn, scale=scale, coeffs=coeffs, intercepts=intercepts
    )

    return evaluate


def get_location_likelihoods(
    loc_df,
    predictors,
    labels=bb_network_decomposition.constants.location_labels,
    evaluation_fn=evaluate_multinomial,
    device="cuda" if torch.cuda.is_available() else "cpu",
):
    X = loc_df[predictors].values.astype(np.float)
    X /= X.std(axis=0)[None, :]

    probs = loc_df[labels].values

    total_counts_used = loc_df["location_descriptor_count"].values
    counts = total_counts_used[:, None] * probs

    X = torch.from_numpy(X.astype(np.float32)).to(device)
    counts = torch.from_numpy(counts.astype(np.float32)).to(device)
    total_counts_used = torch.from_numpy(total_counts_used.astype(np.int)).to(device)

    results = dict()

    for nonlinear in (False, True):
        name = "nonlinear" if nonlinear else "linear"

        log_likelihood, _ = get_fitted_model(
            X, counts, total_counts_used, evaluation_fn, null=False, nonlinear=nonlinear
        )(X, counts, total_counts_used)

        results[f"fitted_{name}"] = log_likelihood.sum().item()
        results[f"fitted_{name}_mean"] = log_likelihood.mean().item()
        results[f"fitted_{name}_lls"] = log_likelihood.detach().cpu().numpy()

    log_likelihood, _ = get_fitted_model(
        X, counts, total_counts_used, evaluation_fn, null=True, nonlinear=False
    )(X, counts, total_counts_used)

    results["null"] = log_likelihood.sum().item()
    results["null_mean"] = log_likelihood.mean().item()
    results["null_lls"] = log_likelihood.detach().cpu().numpy()

    results["rho_mcf_linear"] = bb_network_decomposition.stats.rho_mcf(
        results["fitted_linear"], results["null"]
    )
    results["rho_mcf_nonlinear"] = bb_network_decomposition.stats.rho_mcf(
        results["fitted_nonlinear"], results["null"]
    )

    return results


def get_regression_likelihoods(
    sup_df,
    predictors,
    labels=bb_network_decomposition.constants.supplementary_labels,
    evaluation_fn=evaluate_normal,
    device="cuda" if torch.cuda.is_available() else "cpu",
):
    X = sup_df[predictors].values.astype(np.float)
    X /= X.std(axis=0)[None, :]

    Y = sup_df[labels].values.astype(np.float)
    Y /= Y.std(axis=0)[None, :]

    X = torch.from_numpy(X.astype(np.float32)).to(device)
    Y = torch.from_numpy(Y.astype(np.float32)).to(device)

    results = dict()

    for nonlinear in (False, True):
        name = "nonlinear" if nonlinear else "linear"

        log_likelihood, Y_hat = get_fitted_model(
            X, Y, None, evaluation_fn, null=False, nonlinear=nonlinear
        )(X, Y, None)

        results[f"fitted_{name}_mse"] = sklearn.metrics.mean_squared_error(
            Y.cpu().numpy(), Y_hat
        )
        results[f"fitted_{name}_r2"] = sklearn.metrics.r2_score(Y.cpu().numpy(), Y_hat)
        results[f"fitted_{name}"] = log_likelihood.sum().item()
        results[f"fitted_{name}_mean"] = log_likelihood.mean().item()
        results[f"fitted_{name}_lls"] = log_likelihood.detach().cpu().numpy()

    log_likelihood, _ = get_fitted_model(
        X, Y, None, evaluation_fn, null=True, nonlinear=False
    )(X, Y, None)

    results["null"] = log_likelihood.sum().item()
    results["null_mean"] = log_likelihood.mean().item()

    return results
