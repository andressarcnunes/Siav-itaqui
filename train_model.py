"""
train_model.py
===============
Treina o modelo Random Forest do SIAV-Itaqui para classificar o estado
de operação (normal, microvazamento, ruptura_parcial, ruptura_total) a
partir das leituras de telemetria.

Pré-requisito: rodar src/simulator.py antes, para gerar
data/telemetry_dataset.csv.

Uso:
    python src/train_model.py

Gera:
    models/random_forest_model.joblib   -> modelo treinado
    models/feature_columns.json         -> ordem das features (necessário
                                            para o dashboard fazer previsões
                                            consistentes depois)
    models/confusion_matrix.png         -> gráfico de avaliação
"""

from __future__ import annotations

import json
import os

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report, confusion_matrix, ConfusionMatrixDisplay
import matplotlib.pyplot as plt

from features import engineer_features, get_feature_columns, assign_sequence_ids

DATA_PATH = "data/telemetry_dataset.csv"
MODEL_DIR = "models"
LABELS_ORDER = ["normal", "microvazamento", "ruptura_parcial", "ruptura_total"]


def load_dataset() -> pd.DataFrame:
    if not os.path.exists(DATA_PATH):
        raise FileNotFoundError(
            f"Não encontrei {DATA_PATH}. Rode 'python src/simulator.py' primeiro."
        )
    df = pd.read_csv(DATA_PATH, parse_dates=["timestamp"])
    return df


def split_by_sequence(df: pd.DataFrame, test_size: float = 0.2, seed: int = 42):
    """
    Divide treino/teste por SEQUÊNCIA inteira, não por linha individual.

    Isso é importante: como as features usam janelas móveis (ex.: média
    das últimas 15 leituras), dividir por linha faria uma leitura de
    treino "vazar" informação para uma leitura vizinha do teste. Dividindo
    por sequência completa, garantimos uma avaliação honesta.
    """
    rng = np.random.default_rng(seed)
    sequence_ids = df["sequence_id"].unique()
    rng.shuffle(sequence_ids)

    n_test = int(len(sequence_ids) * test_size)
    test_ids = set(sequence_ids[:n_test])

    is_test = df["sequence_id"].isin(test_ids)
    return df[~is_test].copy(), df[is_test].copy()


def main():
    print("Carregando dataset...")
    df = load_dataset()
    df = assign_sequence_ids(df)

    print("Gerando features (médias móveis, variação, etc.)...")
    df = engineer_features(df)
    feature_cols = get_feature_columns(df)

    train_df, test_df = split_by_sequence(df)
    print(f"Sequências de treino: {train_df['sequence_id'].nunique()} | "
          f"Sequências de teste: {test_df['sequence_id'].nunique()}")

    X_train, y_train = train_df[feature_cols], train_df["label"]
    X_test, y_test = test_df[feature_cols], test_df["label"]

    print("Treinando Random Forest...")
    model = RandomForestClassifier(
        n_estimators=200,
        max_depth=12,
        min_samples_leaf=5,
        class_weight="balanced",  # compensa o desbalanceamento (normal é maioria)
        random_state=42,
        n_jobs=-1,
    )
    model.fit(X_train, y_train)

    print("\nAvaliando no conjunto de teste (sequências nunca vistas no treino)...")
    y_pred = model.predict(X_test)
    print(classification_report(y_test, y_pred, labels=LABELS_ORDER, digits=3))

    # Matriz de confusão (visual)
    cm = confusion_matrix(y_test, y_pred, labels=LABELS_ORDER)
    disp = ConfusionMatrixDisplay(confusion_matrix=cm, display_labels=LABELS_ORDER)
    fig, ax = plt.subplots(figsize=(7, 6))
    disp.plot(ax=ax, cmap="Blues", colorbar=False, xticks_rotation=30)
    plt.title("Matriz de Confusão — SIAV-Itaqui")
    plt.tight_layout()

    os.makedirs(MODEL_DIR, exist_ok=True)
    plt.savefig(os.path.join(MODEL_DIR, "confusion_matrix.png"), dpi=120)
    print(f"Matriz de confusão salva em {MODEL_DIR}/confusion_matrix.png")

    # Importância das features (top 10) -- ajuda a explicar o modelo na banca
    importances = pd.Series(model.feature_importances_, index=feature_cols)
    importances = importances.sort_values(ascending=False)
    print("\nTop 10 features mais importantes para o modelo:")
    print(importances.head(10).to_string())

    # Salva o modelo e a ordem exata das features (o dashboard precisa
    # gerar as features NA MESMA ORDEM para o modelo prever corretamente)
    joblib.dump(model, os.path.join(MODEL_DIR, "random_forest_model.joblib"))
    with open(os.path.join(MODEL_DIR, "feature_columns.json"), "w") as f:
        json.dump(feature_cols, f, indent=2)

    print(f"\nModelo salvo em {MODEL_DIR}/random_forest_model.joblib")
    print(f"Colunas de features salvas em {MODEL_DIR}/feature_columns.json")


if __name__ == "__main__":
    main()
