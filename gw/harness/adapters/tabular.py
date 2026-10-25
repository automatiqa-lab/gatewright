"""Tabular ModelProvider adapters.0004.

- `catboost`: fallback, fitted per run on the same context (Apache-2.0).
- `mitra_v2`: in-context tabular foundation model via `autogluon.tabular` (optional
  extra `gatewright[mitra]`; licence verified at pin time).
- `tabicl_v2`: TabICL run locally (extra `gatewright[tabicl]`).
- `tabpfn`: TabPFN, OFF by default. Its weights are released under a non-commercial licence, so
  the adapter stays down until the package is installed (extra `gatewright[tabpfn-noncommercial]`)
  AND its connection sets `accept_noncommercial_licence: true`.
All are local; none is external.
"""

from __future__ import annotations

import io
from typing import Any

import numpy as np
import pandas as pd

from gw.harness.contracts import Health, Prediction
from gw.harness.registry import register
from gw.record import models as m

SEED = 7


class _Tabular:
    kind = "tabular"

    def complete(self, prompt: str, schema: dict[str, Any] | None, max_tokens: int) -> str | dict[str, Any]:
        raise NotImplementedError

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError

    def is_external(self) -> bool:
        return False


@register("catboost")
class CatBoostAdapter(_Tabular):
    """CatBoost fitted per call: binary -> probabilities, quantile_<a> -> quantile regression."""

    model_id = "catboost"

    def __init__(self, conn: m.Connection | None = None, iterations: int = 300) -> None:
        self.iterations = iterations

    def tabular_fit_predict(self, X_ctx: Any, y_ctx: Any, X_query: Any, task: str) -> Prediction:
        from catboost import CatBoostClassifier, CatBoostRegressor

        cats = [c for c in X_ctx.columns if X_ctx[c].dtype == object]
        if task == "binary":
            model: Any = CatBoostClassifier(iterations=self.iterations, depth=4, learning_rate=0.05, random_seed=SEED,
                                            verbose=False, thread_count=4, allow_writing_files=False)
            model.fit(X_ctx, np.asarray(y_ctx), cat_features=cats)
            p = model.predict_proba(X_query)[:, 1]
        elif task.startswith("quantile_"):
            alpha = float(task.split("_")[1])
            model = CatBoostRegressor(iterations=self.iterations, depth=4, learning_rate=0.05, random_seed=SEED,
                                      loss_function=f"Quantile:alpha={alpha}", verbose=False, thread_count=4,
                                      allow_writing_files=False)
            model.fit(X_ctx, np.asarray(y_ctx), cat_features=cats)
            p = model.predict(X_query)
        else:
            raise ValueError(f"unknown task {task}")
        self.last_model = model
        return Prediction(p=[float(x) for x in p], model_id=self.model_id)

    def health(self) -> Health:
        try:
            import catboost  # noqa: F401

            return Health(status="ok", detail="catboost importable")
        except ImportError as e:
            return Health(status="down", detail=str(e))


@register("mitra_v2")
class MitraAdapter(_Tabular):
    """Mitra-v2 in-context predictions through AutoGluon. Raises when the extra is not installed."""

    model_id = "mitra-v2"

    def __init__(self, conn: m.Connection | None = None) -> None:
        self.device = (conn.config.get("device") if conn else None) or "cpu"

    def tabular_fit_predict(self, X_ctx: Any, y_ctx: Any, X_query: Any, task: str) -> Prediction:
        from autogluon.tabular import TabularPredictor

        df = X_ctx.copy()
        df["__y"] = list(y_ctx)
        problem = "binary" if task == "binary" else "regression"
        pred = TabularPredictor(label="__y", problem_type=problem, verbosity=0).fit(
            df, hyperparameters={"MITRA": {"fine_tune": False, "device": self.device}}, time_limit=300)
        if problem == "binary":
            p = pred.predict_proba(X_query).iloc[:, -1].to_numpy()
        else:
            p = pred.predict(X_query).to_numpy()
        return Prediction(p=[float(x) for x in p], model_id=self.model_id)

    MIN_FREE_GB = 8.0  # AutoGluon estimates about 7 GB for a 5k-row context on CPU

    def health(self) -> Health:
        try:
            import autogluon.tabular  # noqa: F401
            import psutil
        except ImportError:
            return Health(status="down", detail="install the gatewright[mitra] extra")
        free = psutil.virtual_memory().available / 2**30
        if self.device == "cpu" and free < self.MIN_FREE_GB:
            return Health(status="degraded", detail=f"{free:.1f} GB free; Mitra on CPU needs about {self.MIN_FREE_GB:.0f} GB")
        return Health(status="ok", detail=f"autogluon importable, {free:.1f} GB free")


def frame(rows: list[dict[str, Any]], columns: list[str]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=columns)
    for c in columns:
        if df[c].dtype != object:
            df[c] = df[c].astype(float)
    return df


@register("remote_tabular")
class RemoteTabular(_Tabular):
    """In-context tabular models served from the GPU box (gw.harness.tabular_server).

    Config: {url, model: mitra|tabicl|catboost, secret_ref, timeout?}. The bearer token is read by key
    name at call time (never stored). Feature rows carry no free text, so nothing needs redaction,
    and the box is on the private tailnet, so the provider is not external.
    """

    def __init__(self, conn: m.Connection | None = None, url: str = "", model: str = "mitra",
                 token: str | None = None, transport: Any = None) -> None:
        import httpx

        from gw.harness.registry import resolve_config, secret

        cfg = resolve_config(conn.config) if conn else {}
        self.url = (cfg.get("url") or url).rstrip("/")
        self.model = cfg.get("model", model)
        self.model_id = {"mitra": "mitra-v2", "tabicl": "tabicl-v2"}.get(self.model, self.model) + "@gpu"
        self._token = token if token is not None else secret(cfg.get("secret_ref"))
        self.client = httpx.Client(timeout=float(cfg.get("timeout", 1200)), transport=transport)

    def is_external(self) -> bool:
        from gw.harness.adapters.llm import is_local_url

        return not is_local_url(self.url)  # the GPU box behind an SSH tunnel is 127.0.0.1

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token or ''}"}

    def tabular_fit_predict(self, X_ctx: Any, y_ctx: Any, X_query: Any, task: str) -> Prediction:
        ctx = X_ctx.copy()
        ctx["__y"] = list(y_ctx)
        b1, b2 = io.BytesIO(), io.BytesIO()
        ctx.to_parquet(b1, index=False)
        X_query.to_parquet(b2, index=False)
        body = b1.getvalue() + b2.getvalue()
        r = self.client.post(f"{self.url}/fit_predict", params={"model": self.model, "task": task}, content=body,
                             headers={**self._headers(), "x-gw-ctx-bytes": str(len(b1.getvalue())),
                                      "Content-Type": "application/octet-stream"})
        r.raise_for_status()
        return Prediction(p=[float(x) for x in r.json()["p"]], model_id=self.model_id)

    def health(self) -> Health:
        try:
            r = self.client.get(f"{self.url}/health", headers=self._headers(), timeout=5)
            r.raise_for_status()
            info = r.json()
            need = "autogluon.tabular" if self.model == "mitra" else ("tabicl" if self.model == "tabicl" else None)
            if need and not info.get(need):
                return Health(status="down", detail=f"{need} missing on the server")
            return Health(status="ok", detail=f"gpu {info.get('gpu')}")
        except Exception as e:
            return Health(status="down", detail=f"{self.url}: {type(e).__name__}")


@register("tabicl_v2")
class TabICLAdapter(_Tabular):
    """TabICL (BSD-3-Clause, Inria) run locally; on a GPU box use `remote_tabular` with model tabicl."""

    model_id = "tabicl-v2"

    def __init__(self, conn: m.Connection | None = None) -> None:
        self.conn = conn

    def tabular_fit_predict(self, X_ctx: Any, y_ctx: Any, X_query: Any, task: str) -> Prediction:
        import pandas as pd
        from tabicl import TabICLClassifier

        if task != "binary":
            raise ValueError("tabicl_v2 serves binary targets; delay quantiles use catboost")
        cats = [c for c in X_ctx.columns if X_ctx[c].dtype == object]
        both = pd.get_dummies(pd.concat([X_ctx, X_query], keys=["c", "q"]), columns=cats, dtype=float)
        clf = TabICLClassifier(random_state=SEED)
        clf.fit(both.loc["c"].to_numpy(dtype=np.float32), np.asarray(y_ctx).astype(int))
        return Prediction(p=[float(v) for v in clf.predict_proba(both.loc["q"].to_numpy(dtype=np.float32))[:, 1]],
                          model_id=self.model_id)

    def health(self) -> Health:
        try:
            import tabicl  # noqa: F401

            return Health(status="ok", detail="tabicl importable")
        except ImportError:
            return Health(status="down", detail="pip install tabicl (or use remote_tabular on the GPU box)")


@register("tabpfn")
class TabPFNAdapter(_Tabular):
    """TabPFN (Prior Labs). NON-COMMERCIAL weights: off unless the connection opts in explicitly.

    Config: {accept_noncommercial_licence: true, device?: auto|cpu|cuda}. Without the flag the
    adapter reports down and the forecast engine never selects it.
    """

    model_id = "tabpfn"

    def __init__(self, conn: m.Connection | None = None) -> None:
        self.conn = conn
        self.cfg = dict(conn.config) if conn else {}
        self.accepted = self.cfg.get("accept_noncommercial_licence") is True

    def tabular_fit_predict(self, X_ctx: Any, y_ctx: Any, X_query: Any, task: str) -> Prediction:
        if not self.accepted:
            raise PermissionError("tabpfn: non-commercial licence not accepted on this connection")
        if task != "binary":
            raise ValueError("tabpfn serves binary targets; delay quantiles use catboost")
        from tabpfn import TabPFNClassifier

        cats = [c for c in X_ctx.columns if X_ctx[c].dtype == object]
        both = pd.get_dummies(pd.concat([X_ctx, X_query], keys=["c", "q"]), columns=cats, dtype=float)
        clf = TabPFNClassifier(random_state=SEED, device=str(self.cfg.get("device", "auto")))
        clf.fit(both.loc["c"].to_numpy(dtype=np.float32), np.asarray(y_ctx).astype(int))
        return Prediction(p=[float(v) for v in clf.predict_proba(both.loc["q"].to_numpy(dtype=np.float32))[:, 1]],
                          model_id=self.model_id)

    def health(self) -> Health:
        if not self.accepted:
            return Health(status="down", detail="off: set accept_noncommercial_licence: true to opt in")
        try:
            import tabpfn  # noqa: F401

            return Health(status="ok", detail="tabpfn importable | non-commercial licence accepted")
        except ImportError:
            return Health(status="down", detail="pip install gatewright[tabpfn-noncommercial]")
