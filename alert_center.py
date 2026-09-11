"""
alert_center.py
================
Central de Alertas do SIAV-Itaqui — decide QUANDO um alerta deve ser
confirmado (debounce, por berço) e PARA ONDE ele deve ir, conforme a
criticidade (Baixa/Média/Crítica), incluindo o texto simulado/real de
cada mensagem (Dashboard, SMS, WhatsApp e agora E-mail).

Essa lógica antes estava só dentro do dashboard (Etapa 4); agora fica
centralizada aqui, então tanto o dashboard quanto qualquer script de
linha de comando usam exatamente a mesma regra -- sem risco de as duas
pontas divergirem com o tempo.

Nesta fase de hackathon, o envio de SMS/WhatsApp continua SIMULADO (só
registrado em log/tela). O E-MAIL, no entanto, é REAL: usa smtplib da
biblioteca padrão do Python para disparar a mensagem via um servidor
SMTP (Gmail, Outlook, SendGrid, etc.), configurado pelo operador na
barra lateral do dashboard.

Uso rápido (roda uma demonstração no terminal):
    python alert_center.py
"""

from __future__ import annotations

import smtplib
from dataclasses import dataclass, field
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

DEBOUNCE_TICKS = 2  # nº de leituras consecutivas iguais para confirmar uma mudança de estado

# "Email" foi adicionado a todos os níveis que geram alerta (Baixa em
# diante), já que o pedido é notificar a brigada/gestão em qualquer
# Microvazamento ou Ruptura confirmados -- não só nos níveis mais graves.
CRITICALITY_CHANNELS = {
    "Nenhum": [],
    "Baixa": ["Dashboard", "Email"],
    "Média": ["Dashboard", "SMS", "Email"],
    "Crítica": ["Dashboard", "SMS", "WhatsApp", "Email"],
}

_ESTADO_LEGIVEL = {
    "normal": "Operação normal",
    "microvazamento": "Microvazamento",
    "ruptura_parcial": "Ruptura parcial",
    "ruptura_total": "Ruptura total",
}

# Mensagens por canal: SMS curto e direto; WhatsApp com mais contexto e
# instrução operacional; Dashboard é tratado à parte (é sempre um registro
# estruturado, não uma "mensagem" no mesmo sentido).
_TEMPLATES = {
    "Média": {
        "SMS": "SIAV-Itaqui: alerta MEDIO no berco {berco} as {hora}. Possivel {estado_fmt}. Verificar painel.",
    },
    "Crítica": {
        "SMS": "SIAV-Itaqui: ALERTA CRITICO berco {berco} as {hora}. {estado_fmt}. Acionar equipe AGORA.",
        "WhatsApp": (
            "🚨 *ALERTA CRÍTICO — SIAV-Itaqui*\n"
            "Berço: {berco}\n"
            "Horário: {hora}\n"
            "Estado detectado: {estado_fmt}\n"
            "Ação recomendada: bloqueio imediato de válvulas e acionamento "
            "da equipe de emergência no local."
        ),
    },
}

# Templates específicos de e-mail (assunto + corpo), um por nível de
# criticidade que dispara e-mail. Ficam separados de _TEMPLATES porque
# e-mail tem assunto próprio, diferente de SMS/WhatsApp.
_EMAIL_TEMPLATES = {
    "Baixa": {
        "assunto": "[SIAV-Itaqui] Alerta BAIXO — Berço {berco} — {estado_fmt}",
        "corpo": (
            "Alerta de baixa criticidade no SIAV-Itaqui.\n\n"
            "Berço: {berco}\n"
            "Horário: {hora}\n"
            "Estado detectado: {estado_fmt}\n\n"
            "Recomenda-se acompanhar o painel de monitoramento. Nenhuma "
            "ação de emergência é necessária neste momento."
        ),
    },
    "Média": {
        "assunto": "[SIAV-Itaqui] Alerta MÉDIO — Berço {berco} — {estado_fmt}",
        "corpo": (
            "Alerta de criticidade média no SIAV-Itaqui.\n\n"
            "Berço: {berco}\n"
            "Horário: {hora}\n"
            "Estado detectado: {estado_fmt}\n\n"
            "Recomenda-se verificar o painel operacional e preparar a "
            "equipe de manutenção para uma possível intervenção."
        ),
    },
    "Crítica": {
        "assunto": "[SIAV-Itaqui] 🚨 ALERTA CRÍTICO — Berço {berco} — {estado_fmt}",
        "corpo": (
            "ALERTA CRÍTICO no SIAV-Itaqui — ação imediata recomendada.\n\n"
            "Berço: {berco}\n"
            "Horário: {hora}\n"
            "Estado detectado: {estado_fmt}\n\n"
            "Ação recomendada: bloqueio imediato de válvulas e acionamento "
            "da equipe de emergência no local."
        ),
    },
}


@dataclass
class DispatchedAlert:
    timestamp: object
    berco: str
    predicted_label: str
    criticality: str
    channels: list
    messages: dict  # canal -> texto da mensagem
    # Preenchido depois do despacho (o AlertCenter não tem credenciais
    # SMTP; quem envia de fato é a camada de cima, ex.: app.py)
    email_status: str = field(default="")
    # Rastreabilidade do operador de turno responsável no momento do
    # disparo -- exigência da Granel Química para o relatório de SMS.
    operador_nome: str = field(default="")
    operador_matricula: str = field(default="")
    turno: str = field(default="")
    terminal: str = field(default="")


def _bloco_operador(operador: dict | None) -> str:
    """
    Monta um bloco de texto em destaque com a identificação do operador de
    turno responsável, para ser anexado às mensagens de e-mail/notificação.

    `operador` é um dict com as chaves opcionais: nome, matricula, turno,
    terminal (vindo da barra lateral do dashboard). Se vazio/None, retorna
    string vazia -- a mensagem sai sem o bloco em vez de quebrar.
    """
    if not operador or not (operador.get("nome") or operador.get("matricula")):
        return ""

    linhas = [
        "------------------------------------------",
        "OPERADOR DE TURNO RESPONSÁVEL PELO DISPARO",
        "------------------------------------------",
    ]
    if operador.get("nome"):
        linhas.append(f"Nome: {operador['nome']}")
    if operador.get("matricula"):
        linhas.append(f"Matrícula/ID: {operador['matricula']}")
    if operador.get("turno"):
        linhas.append(f"Turno: {operador['turno']}")
    if operador.get("terminal"):
        linhas.append(f"Terminal/Planta: {operador['terminal']}")
    return "\n".join(linhas)


class AlertCenter:
    """
    Mantém o estado de criticidade confirmada por berço (com debounce) e
    gera o despacho simulado/real de alertas para os canais corretos.

    Um objeto por sessão de monitoramento -- o dashboard cria um novo a
    cada simulação iniciada.
    """

    def __init__(self, debounce_ticks: int = DEBOUNCE_TICKS):
        self.debounce_ticks = debounce_ticks
        self._confirmed: dict = {}
        self._pending: dict = {}
        self._pending_count: dict = {}
        self.log: list = []

    def process(self, prediction: dict, operador: dict | None = None) -> DispatchedAlert | None:
        """
        Recebe o resultado de uma previsão do pipeline (dict com berco,
        predicted_label, criticality, timestamp) e retorna um
        DispatchedAlert SE essa leitura confirmar uma MUDANÇA de estado
        (após `debounce_ticks` leituras consecutivas iguais). Caso
        contrário, retorna None -- ainda não há alerta novo a despachar.

        `operador`: dict opcional com a identificação do operador de turno
        no momento do disparo (chaves: nome, matricula, turno, terminal).
        É anexado ao e-mail/notificação e gravado no próprio DispatchedAlert
        para rastreabilidade no log (SQLite/CSV).
        """
        berco = prediction["berco"]
        raw_criticality = prediction["criticality"]

        confirmed = self._confirmed.get(berco, "Nenhum")
        pending = self._pending.get(berco, "Nenhum")
        count = self._pending_count.get(berco, 0)

        if raw_criticality == pending:
            count += 1
        else:
            pending = raw_criticality
            count = 1

        self._pending[berco] = pending
        self._pending_count[berco] = count

        if count < self.debounce_ticks or raw_criticality == confirmed:
            return None

        self._confirmed[berco] = raw_criticality
        channels = CRITICALITY_CHANNELS[raw_criticality]
        hora = prediction["timestamp"].strftime("%H:%M:%S")
        estado_fmt = _ESTADO_LEGIVEL.get(prediction["predicted_label"], prediction["predicted_label"])
        templates = _TEMPLATES.get(raw_criticality, {})
        bloco_operador = _bloco_operador(operador)

        messages = {}
        if "Dashboard" in channels:
            messages["Dashboard"] = f"[{hora}] Berço {berco}: {estado_fmt} — criticidade {raw_criticality}"
        for channel, template in templates.items():
            if channel in channels:
                texto = template.format(berco=berco, hora=hora, estado_fmt=estado_fmt)
                # No WhatsApp (canal mais "rico"), o bloco do operador entra
                # junto para dar contexto completo a quem recebe.
                if channel == "WhatsApp" and bloco_operador:
                    texto = f"{texto}\n\n{bloco_operador}"
                messages[channel] = texto
        if "Email" in channels:
            assunto, corpo = montar_email_alerta(raw_criticality, berco, hora, estado_fmt, operador=operador)
            messages["Email"] = f"Assunto: {assunto}\n\n{corpo}"

        alert = DispatchedAlert(
            timestamp=prediction["timestamp"],
            berco=berco,
            predicted_label=prediction["predicted_label"],
            criticality=raw_criticality,
            channels=channels,
            messages=messages,
            operador_nome=(operador or {}).get("nome", ""),
            operador_matricula=(operador or {}).get("matricula", ""),
            turno=(operador or {}).get("turno", ""),
            terminal=(operador or {}).get("terminal", ""),
        )
        self.log.append(alert)
        return alert

    def confirmed_criticality(self, berco: str) -> str:
        return self._confirmed.get(berco, "Nenhum")


def montar_email_alerta(
    criticality: str, berco: str, hora: str, estado_fmt: str, operador: dict | None = None
) -> tuple[str, str]:
    """
    Monta (assunto, corpo) do e-mail de alerta para uma dada criticidade,
    a partir dos templates centralizados acima. Usado tanto pelo
    AlertCenter.process (para preencher alert.messages["Email"]) quanto
    pelo dashboard, caso precise remontar a mensagem de um alerta antigo.

    Se `operador` for informado, o corpo do e-mail ganha um bloco em
    destaque com a identificação de quem estava no turno no momento do
    disparo (Nome, Matrícula, Turno, Terminal/Planta) -- rastreabilidade
    exigida no relatório de SMS da Granel Química.
    """
    template = _EMAIL_TEMPLATES.get(criticality)
    if template is None:
        assunto = f"[SIAV-Itaqui] Alerta — Berço {berco}"
        corpo = f"Berço: {berco}\nHorário: {hora}\nEstado detectado: {estado_fmt}"
    else:
        assunto = template["assunto"].format(berco=berco, estado_fmt=estado_fmt)
        corpo = template["corpo"].format(berco=berco, hora=hora, estado_fmt=estado_fmt)

    bloco = _bloco_operador(operador)
    if bloco:
        corpo = f"{corpo}\n\n{bloco}"
    return assunto, corpo


def enviar_email_alerta(
    destinatario: str,
    assunto: str,
    mensagem: str,
    *,
    smtp_host: str,
    smtp_user: str,
    smtp_password: str,
    smtp_port: int = 587,
    remetente: str | None = None,
    use_tls: bool = True,
    timeout: int = 10,
) -> tuple[bool, str | None]:
    """
    Envia um e-mail de alerta usando smtplib + email.mime (biblioteca
    padrão do Python -- nenhuma dependência extra).

    Funciona com qualquer provedor SMTP: Gmail (smtp.gmail.com:587, com
    "senha de app"), Outlook/Office365 (smtp.office365.com:587),
    SendGrid, Amazon SES, etc.

    Retorna (sucesso: bool, erro: str | None). Nunca lança exceção --
    isso é proposital, para o dashboard poder mostrar uma mensagem
    amigável em vez de quebrar a simulação por causa de um problema de
    rede/credencial.
    """
    remetente = remetente or smtp_user

    msg = MIMEMultipart()
    msg["From"] = remetente
    msg["To"] = destinatario
    msg["Subject"] = assunto
    msg.attach(MIMEText(mensagem, "plain", "utf-8"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=timeout) as server:
            if use_tls:
                server.starttls()
            server.login(smtp_user, smtp_password)
            server.sendmail(remetente, [destinatario], msg.as_string())
        return True, None
    except Exception as exc:  # smtplib levanta várias exceções diferentes; tratamos todas
        return False, str(exc)


def _demo():
    """Demonstração no terminal: simula uma ruptura total e mostra os despachos (sem enviar e-mail real)."""
    from live_pipeline import SIAVPipeline, simulate_live_feed

    print("Carregando modelo e simulando cenário: ruptura_total...\n")
    pipeline = SIAVPipeline()
    center = AlertCenter()

    for reading in simulate_live_feed(scenario_label="ruptura_total", duration_s=100, seed=11):
        result = pipeline.process_reading(reading)
        alert = center.process(result)
        if alert:
            print(f"=== ALERTA DESPACHADO — criticidade {alert.criticality} ===")
            print(f"Berço {alert.berco} | estado: {alert.predicted_label} | canais: {', '.join(alert.channels)}")
            for canal, msg in alert.messages.items():
                print(f"  [{canal}] {msg}")
            print()

    print(f"Total de alertas despachados na simulação: {len(center.log)}")


if __name__ == "__main__":
    _demo()
