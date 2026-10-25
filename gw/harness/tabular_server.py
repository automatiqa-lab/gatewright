"""Remote tabular inference server for the GPU box.for in-context models that need a GPU: Mitra-v2 (AutoGluon) and TabICL.

Run on the box (tailnet-only bind, bearer token from a 600-mode file, never from the repo):

    python -m gw.harness.tabular_server --host 100.69.202.14 --port 8765

The token is read from $GW_TABULAR_TOKEN or ~/.config/gatewright/tabular.token. Requests carry
parquet bytes (context with a `__y` column, and the query rows); responses are probabilities.
Synthetic or redacted feature rows only: forecast features carry no free text.
"""

from __future__ import annotations

import argparse
import hmac
import io
import os
import tempfile
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException, Request


def _token() -> str:
    env = os.environ.get("GW_TABULAR_TOKEN")
    if env:
        return env
    p = Path.home() / ".config" / "gatewright" / "tabular.token"
    raw = p.read_bytes()
    if raw[:2] in (bytes([0xFF, 0xFE]), bytes([0xFE, 0xFF])):
        raw = raw.decode("utf-16").encode("utf-8")
    # keep only the URL-safe base64 alphabet: drops BOMs, NULs and line ends from shell pipes
    return "".join(ch for ch in raw.decode("utf-8", "ignore") if ch.isascii() and (ch.isalnum() or ch in "-_"))


def fit_predict(model: str, ctx: pd.DataFrame, query: pd.DataFrame, task: str) -> list[float]:
    y = ctx.pop("__y").to_numpy()
    if model == "mitra":
        from autogluon.tabular import TabularPredictor

        df = ctx.copy()
        df["__y"] = y
        problem = "binary" if task == "binary" else "regression"
        with tempfile.TemporaryDirectory() as tmp:
            pred = TabularPredictor(label="__y", problem_type=problem, verbosity=0, path=tmp).fit(
                df, hyperparameters={"MITRA": {"fine_tune": False}}, time_limit=900)
            out = pred.predict_proba(query).iloc[:, -1] if problem == "binary" else pred.predict(query)
            return [float(v) for v in np.asarray(out)]
    if model == "tabicl":
        from tabicl import TabICLClassifier

        cats = [c for c in ctx.columns if ctx[c].dtype == object]
        both = pd.get_dummies(pd.concat([ctx, query], keys=["c", "q"]), columns=cats, dtype=float)
        clf = TabICLClassifier(random_state=7)
        clf.fit(both.loc["c"].to_numpy(dtype=np.float32), y.astype(int))
        return [float(v) for v in clf.predict_proba(both.loc["q"].to_numpy(dtype=np.float32))[:, 1]]
    if model == "catboost":
        from catboost import CatBoostClassifier

        cats = [c for c in ctx.columns if ctx[c].dtype == object]
        m = CatBoostClassifier(iterations=300, depth=4, learning_rate=0.05, random_seed=7, verbose=False,
                               allow_writing_files=False)
        m.fit(ctx, y, cat_features=cats)
        return [float(v) for v in m.predict_proba(query)[:, 1]]
    raise ValueError(f"unknown model {model}")


def create_app() -> FastAPI:
    app = FastAPI(title="Gatewright tabular server")
    token = _token()

    def check(request: Request) -> None:
        got = request.headers.get("authorization", "")
        if not hmac.compare_digest(got.encode(), f"Bearer {token}".encode()):
            raise HTTPException(401, "bad token")

    @app.get("/health")
    def health(request: Request) -> dict[str, Any]:
        check(request)
        info: dict[str, Any] = {"status": "ok"}
        try:
            import torch

            info["gpu"] = torch.cuda.get_device_name(0) if torch.cuda.is_available() else None
        except ImportError:
            info["gpu"] = None
        for mod in ("autogluon.tabular", "tabicl"):
            try:
                __import__(mod)
                info[mod] = True
            except ImportError:
                info[mod] = False
        return info

    @app.post("/fit_predict")
    async def predict(request: Request, model: str, task: str = "binary") -> dict[str, Any]:
        check(request)
        body = await request.body()
        n_ctx = int(request.headers.get("x-gw-ctx-bytes", "0"))
        ctx = pd.read_parquet(io.BytesIO(body[:n_ctx]))
        query = pd.read_parquet(io.BytesIO(body[n_ctx:]))
        t = time.perf_counter()
        p = fit_predict(model, ctx, query, task)
        return {"p": p, "model": model, "seconds": round(time.perf_counter() - t, 2), "n_ctx": len(ctx)}

    return app


def main() -> None:
    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True, help="bind address; use the tailnet IP, never 0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    if a.host in ("0.0.0.0", "::"):
        raise SystemExit("refused: bind the tailnet address only")
    uvicorn.run(create_app(), host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
