# הוראות הפעלה — Harel Terminal

---

## שלב 0 — התקנה (פעם אחת)

```bash
cd Harel-Stocks
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[serve,mcp,dev]"
```

### הגדרת סביבה

```bash
cp .env.example .env
```

ערוך את `.env`. **שדה אחד חובה:**

```bash
SEC_CONTACT_EMAIL=iftachts@gmail.com
```

בלי זה ה־SEC מחזירה 403 ומאבדים את המקור הכי חשוב במערכת. ה־SEC דורשת
User-Agent עם כתובת קשר — זה לא אופציונלי מבחינתם.

טעינה לסביבה (בכל טרמינל חדש):

```bash
set -a && source .env && set +a
```

---

## שלב 1 — אימות לפני שסומכים על משהו

שלוש פקודות, בסדר הזה. **אל תדלג.**

### 1.1 `harel doctor`

```bash
harel doctor
```

מה לחפש:
- `universe  22 active, 0 unresolved` — אם מופיע unresolved, ראה שלב 2
- `sources   20/29 available` — 3 מחכים למפתח API, 6 כבויים ב-`sources.yaml`
  (כל אחד עם `notes:` שמסביר למה). זה תקין
- רשימת "Sources off" — מפתחות שאפשר להוסיף מאוחר יותר

### 1.2 `harel verify-feeds`

```bash
harel verify-feeds
```

עובר על ~31 פידי RSS ומדווח `OK` / `EMPTY` / `DEAD` לכל אחד, עם ה־URL.

**כל פידי ה־IR אומתו מול האתרים החיים** — לא רק שהם מחזירים 200, אלא שמה
שהם מחזירים הוא באמת הודעות לעיתונות. זה לא היה המצב קודם: שמונה מתוך שמונה־עשרה
היו פידי WordPress של כל האתר (בלוג, case studies, תוכן שיווקי) וחמישה החזירו
אפס פריטים. ב־trust 1.0 זה גרוע מכלום — הפיד של BrainsWay הכניס פריט שכותרתו
"Title" כחדשות חברה בנות דקה. שישה הוחלפו בפידי IR אמיתיים, שבעה הוסרו.

אם אתה מוסיף פיד: אל תסתפק ב־`harel verify-feeds` (הוא בודק חיוּת, לא תוכן).
פתח אותו ותסתכל על שלוש הכותרות האחרונות.

תיקון: פתח את `config/universe.yaml`, מצא את הטיקר, החלף את ה־URL תחת `ir_feeds`.
פיד שבור לא מפיל שם — הוא רק יורד לכיסוי דרך Google News (אמון 0.6 במקום 1.0).

### 1.3 `harel probe-maya`

```bash
harel probe-maya
```

בודק את הערוץ הישראלי מקצה לקצה ומדפיס בדיוק מה חזר.

- `OK -> 2026-07-30T06:05:00+00:00 | [MAYA] דיווח מיידי…` → מצוין, הערוץ עובד
- `HTTP 4xx` → ה־endpoint השתנה. פתרון: הירשם ב-`openapi.tase.co.il`, קבל מפתח,
  שים `TASE_API_KEY` ב-`.env`
- `records found but fields did not map` → שמות שדות השתנו. הפלט מציג את המפתחות
  האמיתיים; הוסף אותם ל-`TITLE_KEYS` / `DATE_KEYS` ב-`src/harel/collect/maya.py`

זה המקור בעל הערך הגבוה ביותר בסל הזה (20 מ-22 דואליים) — שווה את שתי הדקות.

---

## שלב 2 — להוסיף או לשנות שם בסל

הסל מוגדר כולו ב-`config/universe.yaml`. אין קוד לשנות.

```yaml
  TICKER:
    name: Full Legal Name Inc
    aliases: ["שם מקוצר", "שם בעברית", "מותג"]
    cik: null                # ייפתר אוטומטית ממפת הטיקרים של ה-SEC
    tase_id: null            # מזהה נייר ב-TASE, אם דואלי
    exchange: NASDAQ
    sector: <מפתח מ-sectors.yaml>
    float_class: micro | small | mid | large
    ir_feeds: [https://…/rss]
    peers: [SYM1, SYM2]
    peer_names: ["Competitor A", "Competitor B"]     # חובה
    themes: ["theme one", "theme two"]               # חובה
    products: { BRAND: ["generic name", "alias"] }
    competitor_products: ["Rival Brand"]
    peer_events_that_matter: ["Customer capex guidance"]
    single_points_of_failure: ["תלות בשותף יחיד"]
```

**השדות שקובעים את איכות הכיסוי העקיף** הם `peer_names`, `competitor_products`
ו-`themes`. בלעדיהם השם ייאסף אבל לא תקבל עליו קריאה צולבת.

אחרי עריכה:

```bash
pytest tests/test_config.py -q
```

הבדיקות דורשות `peer_names` ו-`themes` לכל שם פעיל, ומאמתות שה-`sector` קיים —
כך שרשומה חלקית נופלת בבדיקה במקום לאסוף בשקט חצי מהמידע.

**טיקר שלא נפתר:** אם סימבול הוקפא, נמחק או הוקלד לא נכון, סמן אותו
`unresolved: true` + `enabled: false` עם `resolution_hint`. הוא יופיע כאזהרה
בכל תדריך בוקר במקום להיעלם בשקט.

---

## שלב 3 — איסוף ראשון

```bash
harel collect --hours 336        # שבועיים אחורה, למלא את המסד
```

לוקח 3–10 דקות בפעם הראשונה (rate limiting מכוון מול ה-SEC). הפלט מראה
כמה נאסף מכל מקור, אזהרות, ושגיאות.

בדיקה שיש תוכן:

```bash
harel doctor          # אמור להראות אלפי items
harel morning         # תדריך בוקר
harel feed --limit 20 # הפיד המדורג
```

---

## שלב 4 — הפעלה יומיומית

### הרצה רציפה

```bash
harel watch --interval 300 --hours 12
```

מעבר איסוף כל חמש דקות, חלון מבט של 12 שעות. משאיר את המסד עדכני.
הדפסה של התראות חדשות בכל מעבר.

> **אל תקטין את `--interval` מתחת ל-250 שניות.** מעבר מלא נמדד ב-~250 שניות
> (כ-100 שאילתות Google News ועוד EDGAR, כולן מוגבלות-קצב בכוונה). אינטרוול קצר
> מזמן המעבר פירושו שהלולאה לא ישנה כלל אלא רצה ברצף ומציפה את המקורות.

**כשירות על לינוקס:**

```bash
sudo cp scripts/harel-terminal.service /etc/systemd/system/harel-collect.service
sudo cp scripts/harel-web.service /etc/systemd/system/harel-terminal.service
# ערוך User, WorkingDirectory ונתיב ה-venv בשני הקבצים
sudo systemctl daemon-reload
sudo systemctl enable --now harel-collect harel-terminal
```

**על Windows** (אין systemd) — `scripts/harel-windows.ps1` מריץ את שני
התהליכים יחד, טוען את `.env` לסביבה, וכותב ללוגים תחת `logs/`:

```powershell
# בחזית, בסשן הנוכחי - Ctrl-C עוצר את שניהם
.\scripts\harel-windows.ps1

# שורד logoff/reboot דרך Task Scheduler (פעם אחת)
.\scripts\harel-windows.ps1 -Install
Start-ScheduledTask -TaskName HarelTerminal
```

הסקריפט טוען את `.env` בעצמו מפני שהאפליקציה קוראת `os.environ` ישירות ואינה
מפרסרת `.env`. בלי `SEC_CONTACT_EMAIL` ה-SEC מחזירה 403 ו-EDGAR שותק.

### מעבר אחד בשעה (Windows, מומלץ)

`harel watch` הוא לולאה בתוך תהליך אחד: מה שמפיל את התהליך מפיל איתו את הלו"ז.
`scripts/harel-hourly.ps1` הוא הצורה ההפוכה — מעבר יחיד שמסתיים, ו-Task Scheduler
מחזיק את הקצב. מעבר שנכשל עולה שעה, לא את כל התזמון.

```powershell
.\scripts\harel-hourly.ps1            # מעבר אחד עכשיו, בחזית
.\scripts\harel-hourly.ps1 -Install   # רישום המשימה (פעם אחת)
.\scripts\harel-hourly.ps1 -Uninstall
```

המשימה נרשמת כ-`\Harel\HarelCollectHourly` — **בתיקייה, לא בשורש**. רישום בשורש
דורש הרשאות מנהל; תיקייה לא. מאותה סיבה טריגר ה-logon מוגבל למשתמש הנוכחי:
`-AtLogOn` בלי `-User` פירושו "כל משתמש", וזו פעולה ברמת מנהל.

```powershell
Start-ScheduledTask   -TaskName HarelCollectHourly -TaskPath \Harel\
Get-ScheduledTaskInfo -TaskName HarelCollectHourly -TaskPath \Harel\
```

`LastTaskResult`: `0` נקי, `1` המעבר רץ אבל מקור החזיר שגיאה, `267009` רץ כרגע.
הפלט המלא ב-`logs/hourly.log`.

מעבר מלא נמדד ב-~320 שניות, ולכן שעה היא מרווח נוח — בניגוד ל-300 שניות, שבהן
המעבר הבא מתחיל לפני שהקודם נגמר.

### הטרמינל

```bash
harel serve                  # http://127.0.0.1:8787
```

עמוד אחד, צפוף, ענבר על שחור, **בעברית ומימין לשמאל**: פיד → תנועות ללא
הסבר → התראות חדשות → מנייעים → מאיה לילי → לוח אירועים → אזהרות כיסוי.
הפיד ראשון, ואזהרות הכיסוי אחרונות: הן מצב של הצנרת, לא חדשות של היום.
המצב המלא של המקורות נמצא ב-`/sources`, ושם האזהרות כן פותחות את העמוד.
אין JavaScript, אין רשת חיצונית.

הממשק בעברית; ה-API וה-MCP נשארים באנגלית, כי זו השפה שבה הסוכן מונחה. סימבולים,
מחירים, זמנים ומפתחות מקור עטופים באיים של LTR — בלעדיהם `+4.8%` מוצג כ-`%4.8+`.

לצד כל כותרת יש `why?` → `/item/{uid}`: מאיזו שאילתה היא הגיעה, כמה המקור שווה
ולמה, זמן הפרסום מול הפעמון, כמה זמן לקח לנו לראות אותה, איזה כלל קשר אותה
לטיקר, הניקוד צעד-אחר-צעד, מי עוד נשא את הסיפור, מאיפה הגיע המחיר — וקישורים
חיצוניים לאימות (המסמך המקורי, EDGAR, מאיה, מסך ציטוט).

`/sources` — כל מקור, האמון שלו, כמה החזיר במעבר האחרון ומתי הצליח לאחרונה.
זו התשובה ל"האם המערכת בכלל הסתכלה", שהיא שאלה אחרת מ"האם משהו שבור".

**מאזין רק ל-loopback בכוונה.** אל תחשוף החוצה.

---

## שלב 5 — הפקודות שתשתמש בהן בפועל

```bash
harel morning                      # ← תתחיל כאן כל בוקר
harel morning --hours 16           # רק מאז הסגירה אתמול

harel feed                         # ציון ≥45, 24 שעות אחרונות
harel feed --min-score 70          # רק התראות
harel feed --tickers TEVA,CGEN     # שמות ספציפיים
harel feed --relations DIRECT      # רק חדשות של החברות עצמן
harel feed --relations PRODUCT_RIVAL,PEER   # רק קריאה צולבת
harel feed --events clinical_readout,equity_offering
harel feed --why                   # למה כל פריט קיבל את הציון שלו

harel brief TEVA                   # שם אחד: ישיר + עקיף + מחיר + לוח
harel brief CGEN --hours 168       # שבוע אחורה

harel search "potash contract"     # חיפוש מלא (עברית עובדת)
harel search "דיווח מיידי"
harel search '"export controls" NOT China'   # תחביר FTS5

harel moving                       # מנייעים + ההסבר שלהם
harel moving --min-pct 5

harel explain 3f2a9c1b             # ← כל הראיות מאחורי פריט אחד
                                   #   (ה-uid מודפס אפור מתחת לכל כותרת;
                                   #    10 תווים מספיקים)
harel sources                      # האם המערכת בכלל הסתכלה, לפי מקור
harel rescore                      # החל את הקונפיג הנוכחי על מה שכבר נאסף
                                   #   (בלעדיו כוונון לא נבדק: כותרת ששינית
                                   #    בגללה תקרה שומרת את הציון הישן)

harel export today.html            # תמונת מצב סטטית
```

**דגלים גלובליים:**

| | |
|---|---|
| `--json` | פלט JSON גולמי — לצנרת, לסקריפטים, לסוכן |
| `--db PATH` | מסד אחר (למשל `data/demo.db`) |
| `-v` | לוגים מפורטים, לדיבוג |
| `--no-color` | בלי ANSI |

---

## שלב 6 — לחבר את הסוכן

### דרך MCP (מומלץ)

`~/.claude.json` או הגדרות Claude Desktop:

```json
{
  "mcpServers": {
    "harel": {
      "command": "/full/path/to/Harel-Stocks/.venv/bin/harel",
      "args": ["mcp"],
      "env": { "SEC_CONTACT_EMAIL": "iftachts@gmail.com" }
    }
  }
}
```

הסוכן מקבל 9 כלים: `morning_brief`, `feed`, `ticker_brief`, `search`,
`whats_moving`, `get_item`, `calendar`, `universe`, `health`.

השרת מזריק לסוכן הוראות שאוסרות עליו להציג ידיעה של `PEER` כחדשות של החברה
שלנו, ומחייבות אותו לצטט `source` ו-`url`.

### דרך REST

```bash
harel serve
curl -s localhost:8787/agent/manifest | jq   # הסבר לסוכן איך לקרוא כל שדה
curl -s "localhost:8787/api/feed?tickers=TEVA&min_score=60" | jq
curl -s localhost:8787/api/brief/CGEN | jq
```

---

## שלב 7 — כוונון אחרי שבוע

הכול ב-`config/scoring.yaml`. **אל תיגע בקוד.**

| מה מרגיש לא נכון | מה לשנות |
|---|---|
| הפיד שטחי מדי, מפספס דברים | `recency.half_life_hours` 8 → 12 |
| הפיד מלא בישן | `recency.half_life_hours` 8 → 6 |
| יותר מ-10 התראות ביום | `tiers.alert` 75 → 80 |
| פחות מ-2 התראות ביום | `tiers.alert` 75 → 70 |
| סוג ידיעה מסוים מדורג נמוך מדי | `events.<שם>.base` |
| רעש חוזר שלא נחסם | הוסף שורה ל-`noise.title_patterns` |
| מילת מפתח ספציפית לשם | `overrides.<TICKER>.keyword_boosts` |

**איך מאבחנים:** `harel feed --why` מדפיס את עקבות הניקוד המלאות של כל פריט —
איזה אירוע זוהה, אמון המקור, מכפיל ה-relation, דעיכת הזמן, כל בונוס. משם
ברור מה לשנות.

---

## פתרון תקלות

| תסמין | סיבה | פתרון |
|---|---|---|
| `HTTP 403` מ-`data.sec.gov` | `SEC_CONTACT_EMAIL` לא נטען | `set -a && source .env && set +a` |
| `harel: command not found` | ה-venv לא פעיל | `source .venv/bin/activate` |
| הפיד ריק | לא רץ איסוף | `harel collect --hours 336` |
| `nothing at score >= 45` | סף גבוה מדי ליום שקט | `harel feed --min-score 20` |
| שם ספציפי בלי חדשות | פיד IR שבור | `harel verify-feeds` |
| MAYA לא מחזיר כלום | endpoint השתנה | `harel probe-maya` |
| `harel doctor` מראה fails=5+ | מקור נשבר | ראה `last_error` בפלט |
| הסוכן אומר "אין חדשות" | אולי אין **איסוף** | הוא חייב לקרוא `coverage_warnings` |

**כלל הזהב:** שקט בפיד יכול להיות "אין חדשות" או "המערכת עיוורת". `harel doctor`
ו-`coverage_warnings` בכל תדריך בוקר קיימים בדיוק כדי להבדיל בין השניים.

---

## דמו בלי רשת

לראות את הדירוג והתיוג לפני שמגדירים משהו:

```bash
python scripts/seed_demo.py
harel --db data/demo.db morning --hours 100000
harel --db data/demo.db brief TEVA --hours 100000
harel --db data/demo.db export demo.html
```

תוכן מציאותי אבל מומצא. לא נתוני שוק.

---

## בדיקות

```bash
pytest -q          # 82 בדיקות, רצות בלי רשת מול fixtures מוקלטים
```

הרץ אחרי כל שינוי ב-`config/` — הבדיקות תופסות שגיאות תצורה
(סקטור שמצביע על מקור לא קיים, שם בלי `peer_names`, כלל שהיה מסווג כל 8-K כאירוע
compliance).
