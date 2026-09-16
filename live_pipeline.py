"""
live_pipeline.py
=================
Pipeline que recebe leituras de telemetria UMA DE CADA VEZ (como um sensor
real transmitindo) e devolve a classificação do modelo em tempo real.

Esse é o elo entre o que já temos (modelo treinado em lote) e o dashboard
(Etapa 4): o Streamlit vai chamar este pipeline a cada novo "tick" de
sensor simulado e mostrar o resultado na tela.

Peças principais:
  - SIAVPipeline: mantém um histórico curto de leituras por berço (preciso
    de contexto para calcular médias móveis), devolve a previsão do
    modelo para a leitura mais recente, e aplica um FILTRO DE CONFIANÇA
    (ver CONFIDENCE_THRESHOLD abaixo) para não trocar de estado com base
    em previsões estatisticamente incertas.
  - simulate_live_feed(): gera leituras "chegando aos poucos" reutilizando
    a MESMA física do simulador da Etapa 1 (evita ter duas versões
    divergentes da lógica de vazamento).

Uso rápido (roda uma demonstração no terminal):
    python src/live_pipeline.py
"""

from __future__ import annotations

import json
import os
import time
from collections import deque
from datetime import datetime

import joblib
import numpy as np
import pandas as pd

from features import engineer_features, get_feature_columns
from simulator import generate_scenario, ScenarioConfig

MODEL_DIR = "models"
MAX_HISTORY = 20  # leituras mantidas em memória por berço (cobre as janelas móveis de 5 e 15)

# Mapeamento de estado -> nível de criticidade (usado depois pelo dashboard
# e pela central de alertas, mas já fica centralizado aqui)
CRITICALITY_MAP = {
    "normal": "Nenhum",
    "microvazamento": "Baixa",
    "ruptura_parcial": "Média",
    "ruptura_total": "Crítica",
}

# Limite físico absoluto de pressão estabelecido pela operação (Granel
# Química): acima de 8.0 kgf/cm² (bar) a linha está fora da faixa segura
# (6.0-7.0 bar em operação normal) independentemente do que o modelo de
# ML preveja. Esta é uma trava de segurança determinística (pressostato),
# não uma inferência estatística -- atua como uma segunda camada de
# proteção que nunca deve ser mascarada pelo modelo nem pelo filtro de
# confiança abaixo.
PRESSURE_CRITICAL_LIMIT_BAR = 8.0

# Filtro de confiança estatística: o Random Forest já calcula a
# probabilidade de cada classe (predict_proba). Se a previsão da leitura
# atual tiver confiança MENOR que este limite, o pipeline NÃO troca de
# estado -- mantém o último estado ESTÁVEL (confiante) conhecido por
# aquele berço. Isso não "falsifica" nada: é o próprio modelo dizendo "não
# tenho certeza suficiente", e a decisão de engenharia é não agir sobre
# incerteza, evitando oscilação (flicker) na "zona cinzenta" entre dois
# estados sem nunca inventar um resultado que o modelo não sustente.
CONFIDENCE_THRESHOLD = 0.70


class SIAVPipeline:
    """
    Carrega o modelo treinado uma única vez e processa leituras uma a uma,
    mantendo o histórico recente necessário para calcular as features de
    tendência (médias móveis, variação).
    """

    def __init__(self, model_dir: str = MODEL_DIR, confidence_threshold: float = CONFIDENCE_THRESHOLD):
        # Tenta a pasta models/ primeiro; se não achar, tenta a raiz do
        # projeto (caso os arquivos tenham sido enviados soltos, sem pasta).
        candidates = [model_dir, "."]
        model_path = columns_path = None
        for candidate_dir in candidates:
            mp = os.path.join(candidate_dir, "random_forest_model.joblib")
            cp = os.path.join(candidate_dir, "feature_columns.json")
            if os.path.exists(mp) and os.path.exists(cp):
                model_path, columns_path = mp, cp
                break

        if model_path is None:
            raise FileNotFoundError(
                f"Modelo não encontrado em '{model_dir}/' nem na raiz do projeto. "
                "Rode 'python src/train_model.py' primeiro, ou envie "
                "random_forest_model.joblib e feature_columns.json para o repositório."
            )

        self.model = joblib.load(model_path)
        with open(columns_path) as f:
            self.feature_columns = json.load(f)
        self.confidence_threshold = confidence_threshold

        # um histórico (buffer) separado por berço, já que cada linha tem sua própria tendência
        self._buffers: dict[str, deque] = {}
        # último estado CONFIANTE (>= confidence_threshold) confirmado por berço.
        # Usado como "estado mantido" quando a previsão atual é incerta.
        self._last_stable_label: dict[str, str] = {}

    def _get_buffer(self, berco: str) -> deque:
        if berco not in self._buffers:
            self._buffers[berco] = deque(maxlen=MAX_HISTORY)
        return self._buffers[berco]

    def process_reading(self, reading: dict) -> dict:
        """
        Recebe uma leitura nova (dict com timestamp, berco, pressure_bar,
        flow_m3h, vibration_mms) e devolve a previsão do modelo para ela.

        Retorna um dict com: label previsto (já filtrado por confiança),
        criticidade, probabilidade de cada classe (dados brutos do
        modelo, sem filtro -- útil para depuração/relatório), e a leitura
        original (para o dashboard exibir).

        Duas camadas de estabilização são aplicadas, nesta ordem:

        1) FILTRO DE CONFIANÇA (estatístico): se a probabilidade da classe
           prevista pelo Random Forest for menor que `confidence_threshold`
           (padrão 70%), o pipeline não troca de estado -- mantém o último
           estado estável (confiante) já conhecido para aquele berço. Isso
           suaviza a "zona cinzenta" entre dois estados sem inventar
           nenhum resultado: é uma decisão de não agir sobre incerteza.

        2) TRAVA DE SEGURANÇA DO PRESSOSTATO (física, não-ML): se a
           pressão da leitura ultrapassar `PRESSURE_CRITICAL_LIMIT_BAR`
           (8.0 bar), o sistema força `predicted_label = "ruptura_total"`
           e `criticality = "Crítica"` incondicionalmente -- essa trava
           nunca é suavizada pelo filtro de confiança, pois representa um
           limite físico determinístico da operação, não uma inferência.
        """
        berco = reading["berco"]
        buffer = self._get_buffer(berco)
        buffer.append(reading)

        # Monta um DataFrame só com o histórico deste berço (sequência
        # contínua por definição, já que é o fluxo ao vivo de um sensor)
        df = pd.DataFrame(list(buffer))
        df["sequence_id"] = 1  # buffer é sempre uma única sequência contínua
        df = engineer_features(df)

        last_row = df.iloc[[-1]][self.feature_columns]

        raw_predicted_label = self.model.predict(last_row)[0]
        probabilities = dict(zip(self.model.classes_, self.model.predict_proba(last_row)[0]))
        confidence = probabilities[raw_predicted_label]

        # --- Filtro de confiança (estatístico) ---
        stable_label = self._last_stable_label.get(berco, "normal")
        if confidence >= self.confidence_threshold:
            predicted_label = raw_predicted_label
            self._last_stable_label[berco] = predicted_label
        else:
            # Previsão incerta: mantém o último estado estável em vez de
            # trocar com base numa leitura estatisticamente pouco confiável.
            predicted_label = stable_label

        criticality = CRITICALITY_MAP[predicted_label]

        # --- Trava de segurança do pressostato (regra física, não-ML) ---
        # Sempre tem prioridade máxima -- nunca é suavizada pelo filtro acima.
        if reading["pressure_bar"] > PRESSURE_CRITICAL_LIMIT_BAR:
            predicted_label = "ruptura_total"
            criticality = "Crítica"
            self._last_stable_label[berco] = predicted_label

        return {
            "timestamp": reading["timestamp"],
            "berco": berco,
            "pressure_bar": reading["pressure_bar"],
            "flow_m3h": reading["flow_m3h"],
            "vibration_mms": reading["vibration_mms"],
            "predicted_label": predicted_label,
            "criticality": criticality,
            # Probabilidades BRUTAS do modelo (sem filtro) -- úteis para
            # depuração/relatório, mostram exatamente o que o RF calculou.
            "probabilities": {k: round(float(v), 3) for k, v in probabilities.items()},
            "raw_predicted_label": raw_predicted_label,
            "confidence": round(float(confidence), 3),
        }


def simulate_live_feed(
    scenario_label: str = "ruptura_parcial",
    berco: str = "104",
    duration_s: int = 120,
    sample_interval_s: int = 1,
    seed: int | None = None,
):
    """
    Gera leituras "chegando ao vivo", uma de cada vez, reaproveitando a
    mesma física de vazamento usada no treino (src/simulator.py) -- assim
    a demonstração é consistente com o que o modelo aprendeu.

    scenario_label: "normal", "microvazamento", "ruptura_parcial" ou "ruptura_total"
    """
    rng = np.random.default_rng(seed)
    anomaly_start = {
        "normal": 0.0,
        "microvazamento": 0.3,
        "ruptura_parcial": 0.5,
        "ruptura_total": 0.6,
    }.get(scenario_label, 0.5)

    config = ScenarioConfig(label=scenario_label, duration_s=duration_s, anomaly_start_frac=anomaly_start)
    df = generate_scenario(config, berco, datetime.now(), rng, sample_interval_s)

    for _, row in df.iterrows():
        yield row.to_dict()


def _demo():
    """Roda uma demonstração no terminal: leituras chegando e sendo classificadas."""
    print("Carregando modelo treinado...")
    pipeline = SIAVPipeline()

    print("Simulando leituras ao vivo do berço 104 (cenário: ruptura_parcial)...\n")
    print(f"{'segundo':>7} | {'pressão':>8} | {'previsto':>16} | {'criticidade':>11} | {'confiança':>9}")
    print("-" * 68)

    for i, reading in enumerate(simulate_live_feed(scenario_label="ruptura_parcial", duration_s=90)):
        result = pipeline.process_reading(reading)
        print(
            f"{i:>7} | {result['pressure_bar']:>8.2f} | "
            f"{result['predicted_label']:>16} | {result['criticality']:>11} | "
            f"{result['confidence']:>8.1%}"
        )

    print("\nDemonstração concluída. O pipeline reagiu à queda de pressão em tempo real.")


if __name__ == "__main__":
    _demo()
