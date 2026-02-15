"""Hidden Markov Model for market regime detection (numpy only)."""
from __future__ import annotations

import numpy as np


class HMMRegimeDetector:
    """3-State HMM for market regime detection.

    States:
        0 = Bull  (trending up, low vol)
        1 = Bear  (trending down, high vol)
        2 = Transition (uncertain, regime change in progress)
    """

    STATE_LABELS = {0: "bull", 1: "bear", 2: "transition"}

    def __init__(self, n_states: int = 3, lookback: int = 60, n_bins: int = 7):
        self.n_states = n_states
        self.lookback = lookback
        self.n_bins = n_bins  # discretization bins

        # Initial state probabilities (prior: mostly bull)
        self.pi = np.array([0.70, 0.10, 0.20])

        # Transition matrix: rows = from, cols = to
        # Bull tends to stay bull; bear transitions are usually via transition state
        self.A = np.array([
            [0.90, 0.02, 0.08],  # bull  → bull/bear/transition
            [0.03, 0.85, 0.12],  # bear  → bull/bear/transition
            [0.30, 0.25, 0.45],  # trans → bull/bear/transition
        ])

        # Emission probabilities: n_states x n_bins
        # Bins represent discretized returns: very_neg, neg, slight_neg, neutral, slight_pos, pos, very_pos
        self.B = np.array([
            [0.02, 0.05, 0.08, 0.20, 0.25, 0.25, 0.15],  # bull: skew positive
            [0.20, 0.25, 0.20, 0.15, 0.10, 0.05, 0.05],  # bear: skew negative
            [0.10, 0.12, 0.15, 0.26, 0.15, 0.12, 0.10],  # transition: uniform-ish
        ])

        self._fitted = False
        self._fit_count = 0

    def _discretize(self, spy_data, vix_data=None) -> np.ndarray:
        """Convert price data to discrete observation indices.

        Uses return z-scores binned into n_bins buckets:
        [-inf, -2σ, -1σ, -0.5σ, +0.5σ, +1σ, +2σ, +inf]
        """
        prices = np.asarray(spy_data, dtype=float)
        # Remove NaN
        mask = ~np.isnan(prices)
        prices = prices[mask]

        if len(prices) < 2:
            return np.array([self.n_bins // 2], dtype=int)

        returns = np.diff(prices) / prices[:-1]
        returns = returns[~np.isnan(returns)]

        if len(returns) == 0:
            return np.array([self.n_bins // 2], dtype=int)

        mu = np.mean(returns)
        sigma = np.std(returns)
        if sigma < 1e-12:
            sigma = 1e-6

        z = (returns - mu) / sigma

        # Bin edges: -2, -1, -0.5, 0.5, 1, 2
        edges = [-2.0, -1.0, -0.5, 0.5, 1.0, 2.0]
        obs = np.digitize(z, edges)  # 0..6
        obs = np.clip(obs, 0, self.n_bins - 1)

        return obs.astype(int)

    def _forward(self, obs: np.ndarray):
        """Forward algorithm. Returns alpha matrix and scaling factors."""
        T = len(obs)
        alpha = np.zeros((T, self.n_states))
        scales = np.zeros(T)

        alpha[0] = self.pi * self.B[:, obs[0]]
        scales[0] = alpha[0].sum()
        if scales[0] > 0:
            alpha[0] /= scales[0]

        for t in range(1, T):
            alpha[t] = (alpha[t - 1] @ self.A) * self.B[:, obs[t]]
            scales[t] = alpha[t].sum()
            if scales[t] > 0:
                alpha[t] /= scales[t]

        return alpha, scales

    def _backward(self, obs: np.ndarray, scales: np.ndarray):
        """Backward algorithm."""
        T = len(obs)
        beta = np.zeros((T, self.n_states))
        beta[-1] = 1.0

        for t in range(T - 2, -1, -1):
            beta[t] = self.A @ (self.B[:, obs[t + 1]] * beta[t + 1])
            if scales[t + 1] > 0:
                beta[t] /= scales[t + 1]

        return beta

    def fit(self, observations: np.ndarray, max_iter: int = 20, tol: float = 1e-4):
        """Fit model parameters using Baum-Welch (EM).

        observations: 1D array of discrete observation indices (0..n_bins-1).
        """
        obs = np.asarray(observations, dtype=int)
        obs = np.clip(obs, 0, self.n_bins - 1)
        T = len(obs)

        if T < 3:
            return  # not enough data

        for iteration in range(max_iter):
            # E-step
            alpha, scales = self._forward(obs)
            beta = self._backward(obs, scales)

            # Posterior: gamma[t, i] = P(state_t = i | obs)
            gamma = alpha * beta
            gamma_sum = gamma.sum(axis=1, keepdims=True)
            gamma_sum = np.where(gamma_sum < 1e-300, 1e-300, gamma_sum)
            gamma /= gamma_sum

            # Xi: P(state_t=i, state_{t+1}=j | obs)
            xi = np.zeros((T - 1, self.n_states, self.n_states))
            for t in range(T - 1):
                numer = (alpha[t, :, None] * self.A *
                         self.B[:, obs[t + 1]][None, :] * beta[t + 1, :][None, :])
                denom = numer.sum()
                if denom > 1e-300:
                    xi[t] = numer / denom

            # M-step
            new_pi = gamma[0] / gamma[0].sum()

            xi_sum = xi.sum(axis=0)
            row_sums = xi_sum.sum(axis=1, keepdims=True)
            row_sums = np.where(row_sums < 1e-300, 1e-300, row_sums)
            new_A = xi_sum / row_sums

            new_B = np.zeros_like(self.B)
            for k in range(self.n_bins):
                mask = (obs == k)
                new_B[:, k] = gamma[mask].sum(axis=0)
            b_row_sums = new_B.sum(axis=1, keepdims=True)
            b_row_sums = np.where(b_row_sums < 1e-300, 1e-300, b_row_sums)
            new_B /= b_row_sums

            # Smoothing: prevent zeros
            new_B = new_B * 0.95 + 0.05 / self.n_bins

            # Check convergence
            delta = (np.abs(new_A - self.A).max() +
                     np.abs(new_B - self.B).max())

            self.pi = new_pi
            self.A = new_A
            self.B = new_B

            if delta < tol:
                break

        self._fitted = True
        self._fit_count += 1

    def _viterbi(self, obs: np.ndarray) -> np.ndarray:
        """Viterbi algorithm for most likely state sequence."""
        T = len(obs)
        obs = np.clip(obs, 0, self.n_bins - 1)

        log_pi = np.log(np.maximum(self.pi, 1e-300))
        log_A = np.log(np.maximum(self.A, 1e-300))
        log_B = np.log(np.maximum(self.B, 1e-300))

        V = np.zeros((T, self.n_states))
        ptr = np.zeros((T, self.n_states), dtype=int)

        V[0] = log_pi + log_B[:, obs[0]]

        for t in range(1, T):
            for j in range(self.n_states):
                scores = V[t - 1] + log_A[:, j]
                ptr[t, j] = np.argmax(scores)
                V[t, j] = scores[ptr[t, j]] + log_B[j, obs[t]]

        # Backtrace
        path = np.zeros(T, dtype=int)
        path[-1] = np.argmax(V[-1])
        for t in range(T - 2, -1, -1):
            path[t] = ptr[t + 1, path[t + 1]]

        return path

    def predict(self, observations: np.ndarray = None,
                spy_data=None, vix_data=None) -> dict:
        """Predict current regime.

        Provide either pre-discretized observations OR raw spy_data/vix_data.
        """
        if observations is None:
            if spy_data is not None:
                observations = self._discretize(spy_data, vix_data)
            else:
                return self._default_prediction()

        obs = np.asarray(observations, dtype=int)
        obs = np.clip(obs, 0, self.n_bins - 1)

        if len(obs) == 0:
            return self._default_prediction()

        # Get filtered state probabilities via forward algorithm
        alpha, scales = self._forward(obs)
        probs = alpha[-1]
        prob_sum = probs.sum()
        if prob_sum > 1e-300:
            probs = probs / prob_sum
        else:
            probs = self.pi.copy()

        state = int(np.argmax(probs))
        confidence = float(probs[state])

        # Transition risk: probability of being in or moving to bear
        # P(next=bear) = sum_i P(current=i) * A[i, bear]
        transition_risk = float(probs @ self.A[:, 1])

        return {
            "state": state,
            "state_label": self.STATE_LABELS[state],
            "probabilities": [float(p) for p in probs],
            "transition_risk": transition_risk,
            "confidence": confidence,
        }

    def _default_prediction(self) -> dict:
        """Default prediction when no data available."""
        return {
            "state": 0,
            "state_label": "bull",
            "probabilities": [0.70, 0.10, 0.20],
            "transition_risk": 0.10,
            "confidence": 0.70,
        }
