"""VPA_FACTS: загрузка config/facts.yaml и рендер в компактный текст для модели."""

from dataclasses import dataclass
from pathlib import Path

import yaml

NEEDS = {"NEEDS_DECISION", "NEEDS_DESCRIPTION"}

ACTIVITY = {
    "active": "активная",
    "low": "малоактивная",
    "sleeping": "спящая",
    "unknown": "активность не размечена — не описывай, насколько там живо",
}
FEATURE_STATUS = {
    "live": "работает",
    "building": "в разработке",
    "designing": "проектируется",
    "planned": "планируется",
    "unavailable": "недоступно",
}


@dataclass
class Facts:
    version: str
    data: dict
    text: str

    @property
    def price_line(self) -> str:
        m = self.data["membership"]
        return f"{m['price']} {m['currency']}/{m['period']}"


def _val(v) -> str:
    if v in NEEDS:
        return "НЕ ЗАФИКСИРОВАНО — не придумывай, скажи что правило ещё не принято"
    if isinstance(v, bool):
        return "да" if v else "нет"
    return str(v).strip()


def render(data: dict) -> str:
    m, e, p = data["membership"], data["entry"], data["privacy"]
    lines = [f"# VPA_FACTS (версия {data['facts_version']})", ""]
    lines += ["## Что такое VPA"] + [f"- {k}: {_val(v)}" for k, v in data["vpa"].items()]
    lines += ["", "## Подписка"]
    lines.append(f"- цена: {m['price']} {m['currency']} в {m['period']}")
    lines += [f"- {k}: {_val(v)}" for k, v in m.items() if k not in ("price", "currency", "period")]
    lines += ["", "## Вступление"] + [f"- {k}: {_val(v)}" for k, v in e.items()]
    lines += ["", "## Комнаты (только эти существуют; любой другой комнаты нет)"]
    for r in data["rooms"]:
        about = "описание ещё не готово — назови комнату, но не выдумывай, что там" \
            if r.get("about") in NEEDS else r["about"]
        lines.append(f"- {r['name']} [{ACTIVITY.get(r['activity'], r['activity'])}]: {about}")
    lines += ["", "## Функции и AI-помощники (статус важен: только «работает» существует сейчас)"]
    for f in data["features"]:
        about = f": {f['about']}" if f.get("about") else ""
        lines.append(f"- {f['name']} — {FEATURE_STATUS.get(f['status'], f['status'])}{about}")
    lines += ["", "## Данные и приватность"] + [f"- {k}: {_val(v)}" for k, v in p.items()]
    lines += ["", "## Связь с людьми"] + [f"- {k}: {_val(v)}" for k, v in data["support"].items()]
    return "\n".join(lines)


def load_facts(path: Path) -> Facts:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return Facts(version=str(data["facts_version"]), data=data, text=render(data))


def load_greetings(path: Path) -> list[str]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["greetings"]
