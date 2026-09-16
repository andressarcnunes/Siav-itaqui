"""
features.py
===========
Engenharia de features para o SIAV-Itaqui.

Uma leitura isolada de pressão não diz muito sozinha -- o que denuncia um
vazamento é a TENDÊNCIA: pressão caindo, vazão subindo, variação ficando
maior. Este módulo transforma as leituras brutas do simulador em features
que capturam essa tendência, para o Random Forest conseguir aprender os
padrões de cada cenário.

Também é usado pelo dashboard em tempo real, então a lógica fica
centralizada aqui em vez de duplicada.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

SENSOR_COLUMNS = ["pressure_bar", "flow_m3h", "vibration_mms"]
ROLLING_WINDOWS = (5, 15)  # em número de leituras (segundos, no nosso caso)


def assign_sequence_ids(df: pd.DataFrame, gap_seconds: float = 2.0) -> pd.DataFrame:
    """
    Identifica sequências contínuas de telemetria por berço.
    """
    df = df.sort_values(["berco", "timestamp"]).reset_index(drop=True)
    seq_id = 0
    ids = np.empty(len(df), dtype=int)
    prev_time = None
    prev_berco = None

    for i, row in enumerate(df.itertuples(index=False)):
        gap = (row.timestamp - prev_time).total_seconds() if prev_time is not None else None
        if prev_time is None or row.berco != prev_berco or gap > gap_seconds:
            seq_id += 1
        ids[i] = seq_id
        prev_time = row.timestamp
        prev_berco = row.berco

    df["sequence_id"] = ids
    return df


def engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Gera as features usadas pelo modelo, a partir das leituras brutas.
    """
    if "sequence_id" not in df.columns:
        df = assign_sequence_ids(df)

    df = df.sort_values(["sequence_id", "timestamp"]).reset_index(drop=True)
    grouped = df.groupby("sequence_id", group_keys=False)

    feature_cols = []
    for col in SENSOR_COLUMNS:
        for w in ROLLING_WINDOWS:
            mean_col = f"{col}_roll_mean_{w}"
            std_col = f"{col}_roll_std_{w}"
            df[mean_col] = grouped[col].transform(lambda s, w=w: s.rolling(w, min_periods=1).mean())
            df[std_col] = grouped[col].transform(lambda s, w=w: s.rolling(w, min_periods=1).std().fillna(0))
            feature_cols += [mean_col, std_col]

        delta1_col = f"{col}_delta_1"
        delta5_col = f"{col}_delta_5"
        df[delta1_col] = grouped[col].transform(lambda s: s.diff(1).fillna(0))
        df[delta5_col] = grouped[col].transform(lambda s: s.diff(5).fillna(0))
        feature_cols += [delta1_col, delta5_col]

    feature_cols = SENSOR_COLUMNS + feature_cols

    df["berco_108"] = (df["berco"] == "108").astype(int)
    feature_cols.append("berco_108")

    df.attrs["feature_columns"] = feature_cols
    return df


def get_feature_columns(df: pd.DataFrame) -> list[str]:
    """Retorna a lista de colunas de features geradas por engineer_features()."""
    return df.attrs["feature_columns"]
