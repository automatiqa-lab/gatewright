"""Synthetic message and document texts for history and the labelled corpus.

: structured and prose replies in English
with a share in Dutch, Spanish, French and Portuguese, OOO auto-replies,
delegations, file names that follow and break the naming pattern, newsletters
and unrelated mail.
"""

from __future__ import annotations

import random
from datetime import date

REASONS = {
    "en": ["crane availability", "steel delivery delayed", "waiting for the insurer's comments", "design review moved",
           "factory acceptance test rescheduled", "resource conflict on another site", "permit office backlog",
           "supplier capacity", "late input from the client", "weather on site"],
    "nl": ["levering staal vertraagd", "wachten op opmerkingen verzekeraar", "capaciteit leverancier", "kraan niet beschikbaar"],
    "es": ["retraso en la entrega de acero", "esperando comentarios de la aseguradora", "capacidad del proveedor",
           "acumulación de expedientes"],
    "fr": ["livraison d'acier retardée", "en attente des commentaires de l'assureur", "capacité du fournisseur",
           "revue de conception déplacée"],
    "pt": ["atraso na entrega do aço", "aguardando comentários da seguradora", "capacidade do fornecedor"],
}

PROSE = {
    "CONFIRM": {
        "en": ["Thanks for the reminder. We are on track and will deliver {title} by {due}.",
               "Confirmed - {title} will be with you on {due} as planned.",
               "All good on our side, {title} is progressing and the date of {due} holds.",
               "Yes, we can confirm the {due} date for {title}.",
               "Noted. Delivery of {title} is on schedule for {due}."],
        "nl": ["Bedankt voor de herinnering. We liggen op schema en leveren {title} op {due}.",
               "Bevestigd, {title} komt op {due} zoals gepland."],
        "es": ["Gracias por el recordatorio. Vamos según lo previsto y entregaremos {title} el {due}.",
               "Confirmado, {title} estará listo el {due}."],
        "fr": ["Merci pour le rappel. Nous sommes dans les temps et livrerons {title} le {due}.",
               "Confirmé, {title} sera prêt le {due} comme prévu."],
        "pt": ["Obrigado pelo lembrete. Estamos no prazo e entregaremos {title} em {due}.",
               "Confirmado, {title} estará pronto em {due}."],
    },
    "FLAG": {
        "en": ["Unfortunately we cannot make {due} for {title}. New date {new_due}, reason: {reason}.",
               "Heads up: {title} will slip to {new_due} because of {reason}.",
               "We need to move {title} to {new_due} - {reason}. Sorry for the short notice.",
               "Our realistic date for {title} is now {new_due} ({reason})."],
        "nl": ["Helaas halen we {due} niet voor {title}. Nieuwe datum {new_due}, reden: {reason}.",
               "{title} schuift naar {new_due} vanwege {reason}."],
        "es": ["Lamentablemente no llegamos al {due} para {title}. Nueva fecha {new_due}, motivo: {reason}.",
               "{title} se retrasa al {new_due} por {reason}."],
        "fr": ["Malheureusement nous ne tiendrons pas le {due} pour {title}. Nouvelle date {new_due}, raison : {reason}.",
               "{title} glisse au {new_due} en raison de {reason}."],
        "pt": ["Infelizmente não conseguiremos cumprir {due} para {title}. Nova data {new_due}, motivo: {reason}."],
    },
    "QUESTION": {
        "en": ["Before we finalise {title}, can you confirm which revision of the layout we should use?",
               "Quick question on {title}: does the scope include the mezzanine level?",
               "Could you send the latest site plan? We need it to complete {title}.",
               "Who should receive {title} - you or the project manager directly?"],
        "nl": ["Kunt u bevestigen welke revisie van de layout we moeten gebruiken voor {title}?"],
        "es": ["¿Puede confirmar qué revisión del plano debemos usar para {title}?"],
        "fr": ["Pouvez-vous confirmer quelle révision du plan utiliser pour {title} ?"],
        "pt": ["Pode confirmar qual revisão da planta devemos usar para {title}?"],
    },
    "DELEGATE": {
        "en": ["Please contact {delegate} for {title}, they now own this item.",
               "I have handed {title} over to {delegate}; please chase them from now on.",
               "{delegate} is responsible for {title} going forward, copied here."],
        "nl": ["Neem voor {title} contact op met {delegate}, die is nu verantwoordelijk."],
        "es": ["Por favor contacte a {delegate} para {title}, ahora es el responsable."],
        "fr": ["Merci de contacter {delegate} pour {title}, qui en est désormais responsable."],
        "pt": ["Por favor contate {delegate} sobre {title}, agora é o responsável."],
    },
    "OOO": {
        "en": ["I am out of the office until {back} with limited access to e-mail. For urgent matters contact the office.",
               "Automatic reply: I am on leave and will return on {back}."],
        "nl": ["Ik ben afwezig tot en met {back}. Voor dringende zaken kunt u het kantoor bellen."],
        "es": ["Estoy fuera de la oficina hasta el {back}. Para asuntos urgentes contacte con la oficina."],
        "fr": ["Je suis absent jusqu'au {back}. Pour toute urgence, contactez le bureau."],
        "pt": ["Estou fora do escritório até {back}. Para assuntos urgentes contate o escritório."],
    },
    "DECLINE": {
        "en": ["This is not in our scope; we will not deliver {title}.", "We decline {title} under the current contract."],
        "es": ["Esto no está en nuestro alcance; no entregaremos {title}."],
        "fr": ["Ceci ne fait pas partie de notre périmètre ; nous ne livrerons pas {title}."],
    },
    "UNRELATED": {
        "en": ["Our quarterly newsletter: new warehouse lighting range now available.",
               "Invitation: logistics real estate summit, register now.",
               "Your invoice for October is attached.", "Reminder: office closed on Friday for maintenance.",
               "Webinar: sustainable cold storage design - seats limited."],
        "nl": ["Nieuwsbrief: nieuwe producten voor magazijnverlichting."],
        "es": ["Boletín: nueva gama de iluminación para almacenes."],
        "fr": ["Lettre d'information : nouvelle gamme d'éclairage pour entrepôts."],
        "pt": ["Boletim: nova linha de iluminação para armazéns."],
    },
}

DOC_TYPES = {
    "specification": ("SPEC", "Technical specification for {title}. Section 1 scope. Section 2 requirements. Section 3 acceptance."),
    "permit_decision": ("PERMIT", "Decision on the application for {title}. The permit is granted subject to the conditions below. Reference {ref}."),
    "estimate": ("EST", "Cost estimate class {cls}. Total {amount} EUR. Contingency included. Basis of estimate attached."),
    "schedule_export": ("SCHED", "Schedule export, data date {due}. Activities, relationships and float. DCMA summary."),
    "test_report": ("SAT", "Site acceptance test report for {title}. Results: tests passed with punch items listed."),
    "rfi": ("RFI", "Request for information on {title}. Please clarify the interface and respond by {due}."),
    "change_order": ("CO", "Change order: scope change for {title}. Cost impact {amount} EUR, schedule impact two weeks."),
    "minutes": ("MOM", "Minutes of meeting. Attendees, decisions and actions on {title}."),
    "drawing": ("DWG", "Drawing sheet for {title}, revision {rev}. Title block, grid references, notes."),
    "invoice": ("INV", "Invoice number {ref}. Amount due {amount} EUR. Payment terms 30 days."),
    "other": ("DOC", "General document related to the project."),
}


def fmt_date(d: date, rng: random.Random, lang: str) -> str:
    if lang == "en":
        return rng.choice([d.isoformat(), d.strftime("%d %b"), d.strftime("%d %B %Y")])
    return d.isoformat() if rng.random() < 0.6 else d.strftime("%d/%m/%Y")


def reply_text(rng: random.Random, intent: str, lang: str, title: str, due: date, new_due: date | None,
               reason: str | None, delegate: str | None, structured: bool) -> str:
    """One reply. Structured replies use the reply block lines from the chase template."""
    if structured and intent == "CONFIRM":
        return rng.choice(["CONFIRM", "CONFIRM\n\nThanks.", "confirm", "CONFIRM - on track"])
    if structured and intent == "FLAG" and new_due:
        return f"FLAG - {new_due.isoformat()} {reason or ''}".strip()
    pool = PROSE[intent].get(lang) or PROSE[intent]["en"]
    tmpl = rng.choice(pool)
    return tmpl.format(title=title.lower(), due=fmt_date(due, rng, lang),
                       new_due=fmt_date(new_due or due, rng, lang), reason=reason or "",
                       delegate=delegate or "my colleague", back=fmt_date(due, rng, lang))


def reason(rng: random.Random, lang: str) -> str:
    return rng.choice(REASONS.get(lang) or REASONS["en"])


def doc_name(rng: random.Random, project: str, deliverable: str, doc_type: str, follows_pattern: bool) -> str:
    rev = rng.choice("ABCDE")
    if follows_pattern:
        return f"{project}_{deliverable}_rev{rev}.pdf"
    prefix = DOC_TYPES[doc_type][0]
    return rng.choice([f"{prefix}-{project[-2:]}-{rng.randint(100, 999)}.pdf",
                       f"{deliverable.replace('_', ' ')} final v{rng.randint(1, 4)}.docx",
                       f"scan_{rng.randint(1000, 9999)}.pdf"])


def doc_text(rng: random.Random, doc_type: str, title: str, due: date) -> str:
    return DOC_TYPES[doc_type][1].format(title=title.lower(), ref=f"{rng.randint(2025, 2026)}-{rng.randint(1000, 9999)}",
                                         cls=rng.randint(2, 5), amount=f"{rng.randint(50, 900)},000",
                                         due=due.isoformat(), rev=rng.choice("ABC"))


DELIVERABLE_DOC_TYPE = {
    "permit_ref": "permit_decision", "test_report": "test_report", "link": "schedule_export",
    "statement": "minutes", "document": "specification",
}


def doc_type_for(deliverable_id: str, evidence: str) -> str:
    if "estimate" in deliverable_id or "cost_plan" in deliverable_id:
        return "estimate"
    if "schedule" in deliverable_id:
        return "schedule_export"
    if "design" in deliverable_id or "layout" in deliverable_id or "drawing" in deliverable_id:
        return "drawing"
    return DELIVERABLE_DOC_TYPE.get(evidence, "specification")
