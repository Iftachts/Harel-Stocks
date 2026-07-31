# Harel Terminal

מערכת בסגנון בלומברג לסל של 22 מניות ישראליות/ישראליות-במקור, שנבנתה **לסוחר יומי / קצר-טווח**.
היא אוספת ידיעות ישירות ועקיפות (כולל רגולציה של מתחרים באותו סקטור), מדרגת אותן לפי
"כמה זה יכול להזיז את המניה בשעות הקרובות", ומגישה את התוצאה לסוכן LLM.

A Bloomberg-style news terminal for a fixed 22-name basket, built for **short-term
trading**. It collects direct and indirect news — including regulatory actions on
sector competitors — ranks everything by "can this move the print in the next few
hours", and serves the result to an LLM agent.

---

## הסל / Universe

| | | |
|---|---|---|
| **Pharma / biotech** | TEVA, KMDA, ORMP, CGEN, OPK | BWAY (devices) |
| **Semis / semicap** | TSEM, NVMI, CAMT | |
| **Defense / aero** | ESLT, TATT | |
| **Comms / satcom** | GILT, AUDC, ALLT | |
| **Software** | NICE, LPSN | PERI (adtech), NYAX (payments) |
| **Cybersecurity** | PANW | |
| **Energy / chemicals** | ORA, ICL, KEN | |

22 שמות, כולם נאספים. אין טיקר לא פתור.

> **PANW** נכנס כ־`cybersecurity_platform` עם סט העמיתים שלו (CrowdStrike, Zscaler,
> Fortinet, SentinelOne, Check Point, Wiz) ו־`peer_read_across: 0.80` — פלטפורמות
> אבטחה נסחרות כקבוצה, ומיס של עמית מוריד את כל הקבוצה תוך שעה. בנוסף יש boost
> ל־NGS ARR / RPO ולכל תזוזה של מיקרוסופט בבנדלינג.

---

## התקנה / Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[serve,mcp,dev]"

cp .env.example .env         # ערוך: SEC_CONTACT_EMAIL הוא חובה
set -a && source .env && set +a
```

`SEC_CONTACT_EMAIL` הוא **חובה** — ה־SEC חוסמת לקוחות בלי User-Agent עם כתובת קשר.

### הרצה ראשונה

```bash
harel doctor                  # מה מוגדר, מה חסר, מה שבור
harel verify-feeds            # אילו פידי RSS באמת חיים
harel probe-maya              # אימות הערוץ הישראלי
harel collect --hours 336     # איסוף ראשון, שבועיים אחורה
harel morning                 # תדריך בוקר
harel watch --interval 300    # לולאת איסוף רציפה (מעבר מלא ~250 שניות)
harel serve                   # http://127.0.0.1:8787  ← הטרמינל
```

**איך זה עובד, בשפה פשוטה: [`docs/HOW-IT-WORKS.md`](docs/HOW-IT-WORKS.md)** —
מאיפה מגיע המידע, מה עובר עליו, ומה רואים בסוף. בלי קוד.

**הוראות ההפעלה המלאות: [`docs/RUNBOOK.md`](docs/RUNBOOK.md)** — התקנה, אימות,
הרצה כשירות, חיבור הסוכן, כוונון ופתרון תקלות.

### דמו בלי רשת

```bash
python scripts/seed_demo.py
harel --db data/demo.db morning --hours 100000
harel --db data/demo.db export demo.html
```

---

## הסוכן / The LLM agent

שתי דרכים לחבר סוכן:

**MCP (מומלץ)** — הסוכן מקבל כלים ישירות:

```json
{ "mcpServers": { "harel": { "command": "harel", "args": ["mcp"] } } }
```

כלים: `morning_brief`, `feed`, `ticker_brief`, `search`, `whats_moving`,
`get_item`, `calendar`, `universe`, `health`.

**REST** — `harel serve`, ואז `GET /agent/manifest` מסביר לסוכן איך לקרוא כל שדה.

### מה שהסוכן מקבל בכל פריט

| שדה | משמעות |
|---|---|
| `score` | 0–100, מהותיות **לסוחר יומי** — לא חשיבות כללית |
| `tier` | `ALERT` ≥75 · `HIGH` 55–74 · `NORMAL` 35–54 · מתחת לזה מוסתר |
| `relation` | של מי הידיעה — ראה למטה |
| `why` | הנימוק לקישור, בשפה טבעית, לציטוט |
| `reasons` | עקבות הניקוד המלאות |
| `corroboration` | כמה מקורות בלתי תלויים נשאו את אותו סיפור |
| `events` | סיווג לפי טקסונומיית האירועים |

### `relation` — הלב של "ידיעות עקיפות"

| | |
|---|---|
| `DIRECT` | החברה עצמה |
| `SUBSIDIARY` | ישות מוחזקת (OPC → KEN, Elbit America → ESLT) |
| `PRODUCT_RIVAL` | אותה מולקולה / אותו מנגנון / אותו סוקט — **קריאה צולבת** |
| `CUSTOMER` | לקוח שההוצאה שלו היא ההכנסה שלנו (TSMC → CAMT) |
| `PEER` | מתחרה בשמו — סנטימנט סקטוריאלי, לא עובדה עלינו |
| `SECTOR_REG` | רגולטור שפועל על הסקטור |
| `SECTOR_THEME` | סיפור תמטי שנוגע לסקטור |

הסוכן **חייב** לא להציג `PEER`/`SECTOR_*` כחדשות על החברה שלנו. זה כתוב מפורשות
בהוראות שרת ה־MCP.

---

## איך הניקוד עובד

```
score = base(סוג האירוע)
      × אמון המקור          (מנפיק = 1.0, אגרגטור = 0.6)
      × מכפיל relation      (DIRECT 1.0 … SECTOR_THEME 0.45)
      × רגישות float        (מיקרו 1.25 … לארג' 0.90)
      × דעיכת זמן           (חצי-חיים 8 שעות)
      + בונוס טרום-מסחר / תוך-יומי
      + בונוס מילות מפתח לפי טיקר
      + אישור מהטייפ         (תנועה >3% או ווליום >2× ADV)
      − תקרות רעש
```

**דיכוי רעש** הוא חצי מהערך של המערכת. `S-8`, `424B3`, `Form 144`, `13G`,
הודעות אסיפה, "יציג בכנס", דוחות ESG — כולם מקבלים תקרה קשיחה ולא מגיעים לפיד.
לעומת זאת `NT 10-Q` (איחור בדיווח) **לא** מסווג כרעש — הוא דגל אדום אמיתי.

הכול ב־`config/scoring.yaml`. אין צורך לגעת בקוד כדי לכוון.

---

## המקורות

**מנפיק (אמון 1.0):** SEC EDGAR submissions · SEC EDGAR full-text · פידי IR של החברות · TASE/MAYA
**רגולציה:** Federal Register (+ public inspection, יום מראש) · openFDA · FDA/EMA RSS · ClinicalTrials.gov v2 · DoD contracts · DSCA · FERC · ECHA
**שוק:** Stooq · Yahoo chart
**אגרגטורים (אמון 0.6–0.75):** Google News (אנג׳/עב׳) · Globes · Calcalist

הפירוט המלא: [`docs/SOURCES.md`](docs/SOURCES.md).

### שתי נקודות שהן היתרון האמיתי של הסל הזה

1. **MAYA/TASE.** 20 מתוך 22 השמות דואליים. דיווח מיידי בעברית ב־10:00 שעון ישראל
   הוא מידע שאפשר לסחור עליו בפתיחה של 09:30 ET — שעות לפני שזה מגיע לוויר האמריקאי.
2. **EDGAR full-text.** מוצא את החברות שלנו בתוך הגשות של **אחרים** — סעיף סיכון אצל
   מתחרה, הזמנה שלקוח גילה. זה בדיוק מה שבלומברג עושה עם גרף הישויות שלה,
   ואין לזה תחליף חינמי אחר.

---

## מגבלות — קרא את זה

**[`docs/LIMITATIONS.md`](docs/LIMITATIONS.md)** מפרט בכנות מה המערכת הזאת *לא* נותנת
מול בלומברג, כמה עולה לסגור כל פער, ואיפה לדעתי כדאי לשים כסף ואיפה לא.

התמצית: הפער הגדול ביותר הוא **שינויי המלצות אנליסטים** (אין מקור חינמי אמין),
אחריו **ציטוטים בזמן אמת** ו**לוח דוחות רשמי**. שאר הפערים קטנים או ניתנים לעקיפה.

---

## מבנה

```
config/       universe.yaml · sectors.yaml · sources.yaml · scoring.yaml
src/harel/
  collect/    edgar · federal_register · fda · clinicaltrials · maya · prices · rss
  enrich/     linker (מי) · events (מה) · materiality (כמה חשוב)
  serve/      api (REST) · mcp_server (סוכן) · terminal (HTML)
  pipeline.py db.py  views.py  cli.py
tests/        82 בדיקות, רצות בלי רשת מול fixtures מוקלטים
```

```bash
pytest -q
```
