"""
Hypixel Skyblock – Bazaar Craft Scraper
========================================
Gebruikt discord.py-self om als user client live mee te luisteren.
Het script wacht tot jij het craft commando typt in Discord,
vangt het bot-bericht op, bladert automatisch door alle pagina's,
en geeft daarna een menu om te sorteren en exporteren.

Vereisten:
    pip install discord.py-self tabulate colorama openpyxl python-dotenv

Gebruik:
    1. Maak een .env bestand aan in dezelfde map met:
           DISCORD_USER_TOKEN=jouw_token_hier
           CHANNEL_ID=1246203973170630737
           BOT_ID=1450386248270090302
    2. Draai het script.
    3. Typ je commando in Discord.
    4. Het script pakt het bericht automatisch op en scrapet alle pagina's.
    5. Gebruik daarna het menu om te sorteren en exporteren.
"""

import re
import csv
import json
import asyncio

from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field

import discord
from tabulate import tabulate
from colorama import Fore, Style, init

import os
from dotenv import load_dotenv

load_dotenv()
init(autoreset=True)

# ══════════════════════════════════════════════════════
#  CONFIGURATIE  –  pas dit aan
# ══════════════════════════════════════════════════════

USER_TOKEN  = os.getenv("DISCORD_USER_TOKEN", "")    # Laad token uit .env
CHANNEL_ID  = int(os.getenv("CHANNEL_ID", "0"))       # Channel ID als integer (geen quotes)
BOT_ID      = int(os.getenv("BOT_ID", "0")) or None   # Bot user ID (int) — None = eerste bot die reageert

DELAY        = 5      # Seconden wachten na elke button press
MAX_PAGES    = 50     # Veiligheidsgrens
PAGE_TIMEOUT = 30.0   # Seconden wachten op bericht-update na button press

# Gewichten voor combined score (moeten samen 1.0 zijn)
WEIGHT_PROFIT = 0.5
WEIGHT_VOLUME = 0.5

# ══════════════════════════════════════════════════════
#  DATA MODEL
# ══════════════════════════════════════════════════════

@dataclass
class Material:
    amount: int
    name: str
    def __str__(self): return f"{self.amount}x {self.name}"


@dataclass
class Craft:
    rank:         int
    name:         str
    input_cost:   float
    output_value: float
    volume:       float
    profit:       float
    requires:     str   = ""
    materials:    list  = field(default_factory=list)
    score:        float = 0.0

    def profit_str(self) -> str: return _fmt(self.profit)
    def volume_str(self) -> str: return _fmt(self.volume)
    def cost_str(self)   -> str: return _fmt(self.input_cost)
    def output_str(self) -> str: return _fmt(self.output_value)
    def score_str(self)  -> str: return f"{self.score:.3f}"


# ══════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════

def _parse_coins(raw: str) -> float:
    raw = raw.strip().replace(",", "")
    for suffix, mult in (("B", 1_000_000_000), ("M", 1_000_000), ("K", 1_000)):
        if raw.endswith(suffix):
            return float(raw[:-1]) * mult
    return float(raw)


def _fmt(n: float) -> str:
    if n >= 1_000_000: return f"{n/1_000_000:.2f}M"
    if n >= 1_000:     return f"{n/1_000:.1f}K"
    return str(int(n))


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


# ══════════════════════════════════════════════════════
#  PARSER  –  embed fields
# ══════════════════════════════════════════════════════

MATERIAL_RE = re.compile(r"(\d+)x\s+(.+)")


def parse_embed(message: discord.Message) -> list:
    """Parse crafts direct uit embed fields."""
    if not message.embeds:
        return []

    crafts = []
    embed  = message.embeds[0]

    for field in embed.fields:
        # Naam van het field: "1. Braided Griffin Feather"
        name_match = re.match(r"(\d+)\.\s*(.+)", field.name or "")
        if not name_match:
            continue

        rank = int(name_match.group(1))
        name = name_match.group(2).strip()
        val  = field.value or ""

        # Materials uit code block
        materials = []
        mat_block = re.search(r"```\n?([\s\S]+?)```", val)
        if mat_block:
            for line in mat_block.group(1).strip().splitlines():
                mat = MATERIAL_RE.match(line.strip())
                if mat:
                    materials.append(Material(int(mat.group(1)), mat.group(2).strip()))

        # Requires
        req_match = re.search(r"\*\*Requires:\*\*\s*(.+)", val)
        requires  = req_match.group(1).strip() if req_match else ""

        # Stats
        cost_m  = re.search(r"Input cost:\s*([\d.,KMBT]+)\s*coins", val)
        out_m   = re.search(r"Output value:\s*([\d.,KMBT]+)\s*coins", val)
        vol_m   = re.search(r"Volume:\s*([\d.,KMBT]+)\s*orders/week", val)
        prof_m  = re.search(r"Profit:\s*([\d.,KMBT]+)\s*coins", val)

        if not (cost_m and out_m and vol_m and prof_m):
            continue

        crafts.append(Craft(
            rank         = rank,
            name         = name,
            input_cost   = _parse_coins(cost_m.group(1)),
            output_value = _parse_coins(out_m.group(1)),
            volume       = _parse_coins(vol_m.group(1)),
            profit       = _parse_coins(prof_m.group(1)),
            requires     = requires,
            materials    = materials,
        ))

    return crafts


def get_page_info_embed(message: discord.Message) -> tuple:
    """Haal paginering op uit embed footer of description."""
    if not message.embeds:
        return 1, 1
    embed = message.embeds[0]

    # Zoek in footer
    footer_text = embed.footer.text if embed.footer else ""
    m = re.search(r"Page (\d+)[/ ]+(\d+)", footer_text or "")
    if m:
        return int(m.group(1)), int(m.group(2))

    # Zoek in description
    m = re.search(r"Page (\d+)[/ ]+(\d+)", embed.description or "")
    if m:
        return int(m.group(1)), int(m.group(2))

    return 1, 1


def get_button(message: discord.Message, label_keywords: list):
    """Zoek een button op basis van label keywords."""
    for row in message.components:
        for comp in row.children:
            if isinstance(comp, discord.Button):
                label = (comp.label or "").lower()
                if any(kw in label for kw in label_keywords):
                    return comp
    return None


# ══════════════════════════════════════════════════════
#  COMBINED SCORE
# ══════════════════════════════════════════════════════

def compute_scores(crafts: list) -> None:
    if not crafts:
        return
    min_p, max_p = min(c.profit for c in crafts), max(c.profit for c in crafts)
    min_v, max_v = min(c.volume for c in crafts), max(c.volume for c in crafts)
    rng_p = max_p - min_p or 1
    rng_v = max_v - min_v or 1
    for c in crafts:
        norm_p = (c.profit - min_p) / rng_p
        norm_v = (c.volume - min_v) / rng_v
        c.score = WEIGHT_PROFIT * norm_p + WEIGHT_VOLUME * norm_v


# ══════════════════════════════════════════════════════
#  SORTERING
# ══════════════════════════════════════════════════════

SORT_KEYS = {
    "profit":   lambda c: c.profit,
    "volume":   lambda c: c.volume,
    "combined": lambda c: c.score,
    "cost":     lambda c: c.input_cost,
    "output":   lambda c: c.output_value,
    "rank":     lambda c: c.rank,
}
SORT_LABELS = {
    "profit":   "Profit          (hoog → laag)",
    "volume":   "Volume          (hoog → laag)",
    "combined": f"Beste combinatie  [profit {int(WEIGHT_PROFIT*100)}% + volume {int(WEIGHT_VOLUME*100)}%]",
    "cost":     "Input cost      (hoog → laag)",
    "output":   "Output value    (hoog → laag)",
    "rank":     "Rank            (originele volgorde)",
}
SORT_MENU = {"1": "profit", "2": "volume", "3": "combined",
             "4": "cost",   "5": "output", "6": "rank"}


def sort_crafts(crafts: list, sort_by: str) -> list:
    return sorted(crafts, key=SORT_KEYS.get(sort_by, SORT_KEYS["profit"]),
                  reverse=(sort_by != "rank"))


# ══════════════════════════════════════════════════════
#  DISCORD CLIENT
# ══════════════════════════════════════════════════════

class CraftScraper(discord.Client):

    def __init__(self):
        super().__init__()
        self.target_channel_id = CHANNEL_ID
        self.bot_id            = BOT_ID
        self.all_crafts: list  = []
        self.seen_ranks: set   = set()
        self._scraping         = False
        self._tracked_message  = None
        self._page_event       = asyncio.Event()
        self._waiting_for_bot  = False

    def _get_content(self, message: discord.Message) -> str:
        """Haal tekst op uit alle embed velden of gewone content."""
        if message.embeds:
            embed = message.embeds[0]
            parts = []
            if embed.title:
                parts.append(embed.title)
            if embed.description:
                parts.append(embed.description)
            for field in embed.fields:
                if field.name:
                    parts.append(field.name)
                if field.value:
                    parts.append(field.value)
            return "\n".join(parts)
        return message.content or ""

    async def on_ready(self):
        print(Fore.GREEN + f"  ✓ Ingelogd als {self.user}")
        print(Fore.CYAN  + f"  Luisteren in kanaal {self.target_channel_id}...")
        print(Fore.YELLOW + "  Typ nu je craft commando in Discord.\n")

    async def on_message(self, message: discord.Message):
        if message.channel.id != self.target_channel_id:
            return

        # Jouw eigen bericht → vlag zetten
        if message.author.id == self.user.id:
            print(Fore.YELLOW + "  Jouw commando gedetecteerd, wachten op bot-reactie...")
            self._waiting_for_bot = True
            return

        # Alleen verwerken als WIJ net een commando stuurden
        if not self._waiting_for_bot:
            return
        if not message.author.bot:
            return
        if self.bot_id and message.author.id != self.bot_id:
            return
        if self._scraping:
            return

        self._waiting_for_bot = False

        # Sla het bericht op — craft data komt mogelijk direct of via edit
        if not self._tracked_message:
            self._tracked_message = message
            content = self._get_content(message)
            print(Fore.YELLOW + "  Bot-bericht onthouden, wachten op craft data...")
            if "Profit" in content:
                self._scraping = True
                asyncio.create_task(self._scrape(message))

    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if after.channel.id != self.target_channel_id:
            return
        if not after.author.bot:
            return
        if self.bot_id and after.author.id != self.bot_id:
            return

        # DEBUG — footer en content tonen
        if after.embeds:
            e = after.embeds[0]
            print(f"  [EMBED] title={e.title!r}")
            print(f"  [EMBED] description={str(e.description)[:80]!r}")
            if e.footer:
                print(f"  [FOOTER] {e.footer.text!r}")
            for i, f in enumerate(e.fields[:2]):
                print(f"  [FIELD {i}] name={f.name!r} value={f.value[:60]!r}")

        content = self._get_content(after)
        print(f"  [EDIT] content={content[:80]!r}")

        if not content:
            return

        # Pagina-update tijdens scrapen
        if self._tracked_message and after.id == self._tracked_message.id:
            self._tracked_message = after
            self._page_event.set()
            if self._scraping:
                return

        # Eerste keer: craft data binnengekregen via edit
        if self._scraping:
            return
        if "Profit" not in content:
            return

        print(Fore.CYAN + f"\n  Craft data ontvangen van {after.author}!")
        self._scraping        = True
        self._tracked_message = after
        asyncio.create_task(self._scrape(after))

    async def _scrape(self, message: discord.Message):
        print(Fore.CYAN + "  Scrapen gestart...\n")

        # Ga naar pagina 1
        for _ in range(MAX_PAGES):
            cur, _ = get_page_info_embed(message)
            if cur <= 1:
                break
            prev_btn = get_button(message, ["prev", "←", "◀", "<"])
            if not prev_btn:
                break
            await prev_btn.click()
            message = await self._wait_for_update()
            if not message:
                break

        # Pagina 1 t/m einde
        for _ in range(MAX_PAGES):
            cur, total = get_page_info_embed(message)
            print(Fore.GREEN + f"  Pagina {cur}/{total}...", end=" ", flush=True)

            new = 0
            for craft in parse_embed(message):
                if craft.rank not in self.seen_ranks:
                    self.all_crafts.append(craft)
                    self.seen_ranks.add(craft.rank)
                    new += 1
            print(f"({new} crafts)")

            if cur >= total:
                print(Fore.CYAN + "  Laatste pagina bereikt.")
                break

            next_btn = get_button(message, ["next", "→", "▶", ">"])
            if not next_btn:
                print(Fore.RED + "  Geen 'next' knop gevonden, stoppen.")
                break

            await next_btn.click()
            message = await self._wait_for_update()
            if not message:
                print(Fore.RED + "  Timeout — stoppen.")
                break

        compute_scores(self.all_crafts)
        print(Fore.GREEN + Style.BRIGHT +
              f"\n  ✓ {len(self.all_crafts)} crafts gescraped & gescoord!")
        await self.close()

    async def _wait_for_update(self):
        """Wacht tot on_message_edit het event triggert."""
        self._page_event.clear()
        await asyncio.sleep(DELAY)
        try:
            await asyncio.wait_for(self._page_event.wait(), timeout=PAGE_TIMEOUT)
        except asyncio.TimeoutError:
            return None
        return self._tracked_message


# ══════════════════════════════════════════════════════
#  DISPLAY
# ══════════════════════════════════════════════════════

def display_table(crafts: list, sort_by: str = "profit") -> None:
    sorted_crafts = sort_crafts(crafts, sort_by)
    show_score    = (sort_by == "combined")
    rows = []
    for c in sorted_crafts:
        mats = " | ".join(str(m) for m in c.materials)
        req  = f" [{c.requires}]" if c.requires else ""
        row  = [c.rank, c.name + req, c.cost_str(), c.output_str(),
                c.volume_str(), c.profit_str(), mats]
        if show_score:
            row.insert(5, c.score_str())
        rows.append(row)

    headers = ["#", "Craft", "Cost", "Output", "Volume/wk", "Profit", "Materials"]
    if show_score:
        headers.insert(5, "Score")

    print()
    print(Fore.CYAN + Style.BRIGHT + f"  ▶  {SORT_LABELS[sort_by]}")
    print(tabulate(rows, headers=headers, tablefmt="rounded_outline"))
    print(Fore.YELLOW +
          f"    {len(crafts)} crafts  |  "
          f"Top profit: {max(crafts, key=lambda c: c.profit).profit_str()}  |  "
          f"Top volume: {max(crafts, key=lambda c: c.volume).volume_str()}")


# ══════════════════════════════════════════════════════
#  EXPORTS
# ══════════════════════════════════════════════════════

def export_csv(crafts: list, sort_by: str = "profit") -> Path:
    path = Path(f"bazaar_crafts_{_timestamp()}.csv")
    with path.open("w", newline="", encoding="utf-8") as f:
        fieldnames = ["rank", "name", "requires", "input_cost",
                      "output_value", "volume", "profit", "score", "materials"]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for c in sort_crafts(crafts, sort_by):
            w.writerow({
                "rank": c.rank, "name": c.name, "requires": c.requires,
                "input_cost": c.input_cost, "output_value": c.output_value,
                "volume": c.volume, "profit": c.profit,
                "score": round(c.score, 4),
                "materials": " | ".join(str(m) for m in c.materials),
            })
    return path


def export_json(crafts: list, sort_by: str = "profit") -> Path:
    path = Path(f"bazaar_crafts_{_timestamp()}.json")
    data = [{
        "rank": c.rank, "name": c.name, "requires": c.requires,
        "input_cost": c.input_cost, "output_value": c.output_value,
        "volume": c.volume, "profit": c.profit, "score": round(c.score, 4),
        "materials": [{"amount": m.amount, "name": m.name} for m in c.materials],
    } for c in sort_crafts(crafts, sort_by)]
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def export_excel(crafts: list, sort_by: str = "profit") -> Path:
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        print(Fore.RED + "  openpyxl niet gevonden → pip install openpyxl")
        return None

    path = Path(f"bazaar_crafts_{_timestamp()}.xlsx")
    wb   = openpyxl.Workbook()
    ws   = wb.active
    ws.title = "Bazaar Crafts"

    hdr_fill   = PatternFill("solid", fgColor="1E3A5F")
    hdr_font   = Font(bold=True, color="FFFFFF", name="Calibri", size=11)
    even_fill  = PatternFill("solid", fgColor="E8F5E9")
    odd_fill   = PatternFill("solid", fgColor="F5F5F5")
    score_fill = PatternFill("solid", fgColor="FFF9C4")
    b          = Side(style="thin", color="CCCCCC")
    border     = Border(left=b, right=b, top=b, bottom=b)
    right      = Alignment(horizontal="right", vertical="center")
    center     = Alignment(horizontal="center", vertical="center")

    col_headers = ["#", "Craft naam", "Requires", "Input cost (coins)",
                   "Output value (coins)", "Volume/week",
                   "Score (combined)", "Profit (coins)", "Materials"]
    col_widths  = [5, 32, 20, 20, 22, 14, 16, 20, 52]

    for col, (h, w) in enumerate(zip(col_headers, col_widths), 1):
        cell           = ws.cell(row=1, column=col, value=h)
        cell.font      = hdr_font
        cell.fill      = hdr_fill
        cell.alignment = center
        cell.border    = border
        ws.column_dimensions[get_column_letter(col)].width = w
    ws.row_dimensions[1].height = 22

    for ri, c in enumerate(sort_crafts(crafts, sort_by), 2):
        base = even_fill if ri % 2 == 0 else odd_fill
        row  = [
            c.rank,
            c.name + (f" [{c.requires}]" if c.requires else ""),
            c.requires, c.input_cost, c.output_value, c.volume,
            round(c.score, 4), c.profit,
            " | ".join(str(m) for m in c.materials),
        ]
        for ci, val in enumerate(row, 1):
            cell        = ws.cell(row=ri, column=ci, value=val)
            cell.fill   = score_fill if ci == 7 else base
            cell.border = border
            if ci in (1, 4, 5, 6, 7, 8):
                cell.alignment = right
            if ci in (4, 5, 8):
                cell.number_format = "#,##0"
            if ci == 7:
                cell.number_format = "0.000"

    ws.freeze_panes    = "A2"
    ws.auto_filter.ref = f"A1:I{ws.max_row}"
    tr = ws.max_row + 1
    ws.cell(row=tr, column=1, value="TOTAAL").font = Font(bold=True)
    ws.cell(row=tr, column=8,
            value=f"=SUM(H2:H{tr-1})").number_format = "#,##0"
    wb.save(path)
    return path


def export_markdown(crafts: list, sort_by: str = "profit") -> Path:
    path  = Path(f"bazaar_crafts_{_timestamp()}.md")
    label = SORT_LABELS[sort_by]
    lines = [
        "# Hypixel Bazaar Crafts",
        f"_Geëxporteerd op {datetime.now().strftime('%d-%m-%Y %H:%M')} · {label}_\n",
        "| # | Craft | Requires | Score | Cost | Output | Volume/wk | Profit | Materials |",
        "|---|-------|----------|-------|------|--------|-----------|--------|-----------|",
    ]
    for c in sort_crafts(crafts, sort_by):
        mats = " \\| ".join(str(m) for m in c.materials)
        lines.append(
            f"| {c.rank} | {c.name} | {c.requires} | {c.score_str()} | "
            f"{c.cost_str()} | {c.output_str()} | {c.volume_str()} | {c.profit_str()} | {mats} |"
        )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


EXPORTS = {
    "A": ("CSV",      export_csv),
    "B": ("JSON",     export_json),
    "C": ("Excel",    export_excel),
    "D": ("Markdown", export_markdown),
}


# ══════════════════════════════════════════════════════
#  MENU
# ══════════════════════════════════════════════════════

DIV = Fore.CYAN + "  " + "─" * 50


def _print_menu(sort_by: str) -> None:
    print()
    print(DIV)
    print(Fore.CYAN + Style.BRIGHT + "  SORTEREN")
    print(DIV)
    for key, val in SORT_MENU.items():
        active = Fore.GREEN + Style.BRIGHT + "  ◄ actief" if val == sort_by else ""
        print(f"  {Fore.WHITE + Style.BRIGHT}[{key}]{Style.RESET_ALL}  {SORT_LABELS[val]}{active}")
    print()
    print(DIV)
    export_label = SORT_LABELS[sort_by].split("(")[0].strip()
    print(Fore.CYAN + Style.BRIGHT + "  EXPORTEREN" +
          Fore.YELLOW + f"  (sorteer: {export_label})")
    print(DIV)
    for key, (label, _) in EXPORTS.items():
        print(f"  {Fore.WHITE + Style.BRIGHT}[{key}]{Style.RESET_ALL}  {label}")
    print()
    print(f"  {Fore.WHITE + Style.BRIGHT}[0]{Style.RESET_ALL}  Afsluiten")
    print(DIV)


def main_menu(crafts: list) -> None:
    sort_by = "profit"
    display_table(crafts, sort_by)

    while True:
        _print_menu(sort_by)
        keuze = input(Fore.WHITE + "\n  Keuze: ").strip().upper()

        if keuze == "0":
            print(Fore.CYAN + "\n  Tot ziens!\n")
            break
        elif keuze in SORT_MENU:
            sort_by = SORT_MENU[keuze]
            display_table(crafts, sort_by)
        elif keuze in EXPORTS:
            label, fn = EXPORTS[keuze]
            print(Fore.YELLOW + f"\n  Exporteren als {label}...")
            result = fn(crafts, sort_by)
            if result:
                print(Fore.GREEN + Style.BRIGHT + f"  ✓ Opgeslagen: {result.resolve()}\n")
        else:
            print(Fore.RED + "  Ongeldige keuze, probeer opnieuw.")


# ══════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════

if __name__ == "__main__":
    print()
    print(Fore.CYAN + Style.BRIGHT + "  ╔════════════════════════════════════════╗")
    print(Fore.CYAN + Style.BRIGHT + "  ║   Hypixel Bazaar Craft Scraper  🪙    ║")
    print(Fore.CYAN + Style.BRIGHT + "  ╚════════════════════════════════════════╝")
    print()

    if not USER_TOKEN:
        print(Fore.RED + "  Geen token gevonden! Maak een .env bestand aan met:")
        print(Fore.YELLOW + "  DISCORD_USER_TOKEN=" + os.getenv("DISCORD_USER_TOKEN", ""))
        exit(1)

    client = CraftScraper()
    client.run(USER_TOKEN)

    if not client.all_crafts:
        print(Fore.RED + "\n  Geen crafts gevonden. Controleer je configuratie.")
    else:
        main_menu(client.all_crafts)