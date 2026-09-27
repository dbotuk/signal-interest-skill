# Interest Signals

`Interest Signals` — самостійна Agent Skill для дослідження того, як змінюється увага до тем у часі та між порівнюваними сегментами. Вона допомагає командам B2C-продуктів сформувати shortlist тем або аудиторій для подальшої перевірки, але не підміняє аналіз ринку, конверсії чи готовності платити.

Поточна версія використовує Wikimedia Pageviews як перше джерело: збирає історію переглядів статей, нормалізує її між мовними розділами, оцінює тренд і надійність сигналу, за потреби запускає додаткові діагностичні модулі та створює аудований набір даних, графіків і коротких звітів.

## Що вирішує skill

Skill підтримує запити на кшталт:

- порівняти динаміку інтересу до однієї теми між мовами;
- оцінити, чи зростає увага до теми і наскільки цьому висновку можна довіряти;
- знайти сезонність, аномальні місяці, можливі структурні зміни або нестабільність;
- ранжувати кілька мовних аудиторій за зростанням, масштабом і якістю доказів;
- створити JSON, CSV, SVG, HTML і односторінковий PDF;
- повторити аналіз після зміни періоду, метрики, ваг або відповідностей статей, використовуючи збережений request і локальний cache.

Результат формулюється як сигнал для наступної валідації. Зростання переглядів не подається як доказ ринкового попиту, розміру ринку, конверсії чи готовності купувати.

## Архітектура

Рішення поділено на runtime skill і development/evaluation середовище:

```text
.
├── interest-signals/               самостійний runtime skill
│   ├── SKILL.md                     workflow та правила для агента
│   ├── agents/openai.yaml           UI metadata
│   ├── references/                  методологія, planner і request contract
│   ├── scripts/analyze.py           CLI entrypoint
│   ├── scripts/interest_signals/    resolution, collection, metrics, reports
│   └── requirements.lock.txt        pinned PDF dependencies
└── interest-signals-dev/           evaluator-side матеріали
    ├── baselines/                   перевірений deterministic baseline
    ├── docs/                        eval protocol і forward-test provenance
    ├── evals/                       cases, fixtures, rubrics, schemas, runner
    └── tests/                       unit та integration tests
```

До агента передається лише `interest-signals/`. Eval prompts, expected behavior, rubrics, thresholds і baselines залишаються поза runtime skill, щоб не підказувати агенту правильну відповідь.

## Робочий процес

```text
Запит користувача
        │
        ▼
Формалізація питання, мов, періоду, метрики та критеріїв
        │
        ▼
Resolution статей і перевірка семантичної відповідності
        │
        ▼
Збір і кешування щоденних Pageviews
        │
        ▼
Агрегація в календарні місяці та перевірка якості даних
        │
        ├── відтворюваний core analysis
        └── question-aware optional modules
        │
        ▼
Evidence quality, warnings і ranking
        │
        ▼
JSON / CSV / SVG / HTML / PDF
        │
        ▼
Обережний висновок і наступний крок валідації
```

### 1. Формування request

CLI приймає flags або JSON config. Основні поля:

- `topic` — тема дослідження;
- `question` — конкретне дослідницьке питання;
- `source_language` — мова, через яку ідентифікується концепт;
- `languages` — 1–12 мовних сегментів;
- `titles` — ручні title overrides;
- `start`, `end` — inclusive UTC range;
- `metric` — `raw` або `share`;
- `analysis_modules` — `auto` або явний список модулів;
- `weights` — ваги growth, volume і reliability;
- `formats` — `csv`, `svg`, `html`, `pdf`;
- `bootstrap_samples` і `seed` — параметри відтворюваності.

За замовчуванням використовується останні 24 повні календарні місяці, `raw` для однієї мови, `share` для порівняння мов і `analysis_modules: ["auto"]`.

Повний контракт описаний у [`request-format.md`](interest-signals/references/request-format.md).

### 2. Resolution перед збором даних

Pipeline спочатку знаходить і перевіряє статті, а лише потім завантажує історію переглядів. Він:

- canonicalize redirects;
- відхиляє disambiguation pages;
- отримує Wikidata QID;
- використовує точні interlanguage links;
- перевіряє QID для ручних title overrides;
- записує resolution method, confidence, candidates і warnings.

Відсутній interlanguage link не трактується як нульовий інтерес. Якщо відповідність не вдалося підтвердити, мова залишається unresolved або користувач має передати перевірений `--title LANG=ARTICLE`.

### 3. Збір і підготовка даних

Поточний adapter використовує MediaWiki Action API та Wikimedia Pageviews API з однаковим фільтром `all-access/user` для article і project totals.

Запити:

- виконуються послідовно;
- мають descriptive User-Agent;
- підтримують bounded retries і `Retry-After`;
- кешуються за повним URL;
- діляться на обмежені часові частини;
- агрегуються з денних значень у календарні місяці локально.

Місяці мають явний `data_status`: `observed`, `implicit_zero_days`, `not_created`, `creation_partial`, `project_data_gap` або `provisional`. Недоступні й неповні періоди не перетворюються на нулі та виключаються з inference.

### 4. Відтворюваний core analysis

Для кожного series завжди розраховуються:

- annualized log-linear trend;
- deterministic moving-block bootstrap interval;
- trailing 12-month growth за наявності 24 contiguous months;
- recent 3-month momentum за наявності 6 contiguous months;
- частка найбільшого місяця;
- trend після вилучення найбільшого місяця;
- event sensitivity;
- evidence quality.

Використовуються дві метрики:

- `raw` — абсолютні перегляди статті;
- `share` — перегляди статті на один мільйон переглядів відповідного мовного проєкту.

Raw показує масштаб, а share — відносну увагу всередині мовного сегмента. Якщо їхні тренди мають протилежні напрями, pipeline створює окремий warning.

### 5. Adaptive analysis planner

Core analysis завжди однаковий для порівнюваності, але повний аналіз не є статичним. Planner вибирає optional modules відповідно до питання користувача або core warning:

| Модуль | Коли вибирається | Мінімальні дані |
| --- | --- | --- |
| `seasonality` | сезонність, цикли, recurring patterns | 24 contiguous months і два повні цикли |
| `changepoints` | різка зміна, regime shift, до/після | 12 contiguous months, по 6 з кожного боку |
| `anomalies` | spikes, events, unusual months | 12 eligible months |
| `stability` | стабільність, волатильність | 6 eligible months |

Режими planner:

- `baseline_only` — спеціалізований модуль не потрібен;
- `adaptive` — модуль вибрано за question intent або core diagnostic;
- `explicit` — користувач явно назвав потрібні модулі.

Кожен модуль повертає `computed` або структурований `skipped` із причиною. Optional analysis не змінює core metrics і ranking без окремо валідованої політики.

Деталі містяться в [`adaptive-analysis.md`](interest-signals/references/adaptive-analysis.md).

### 6. Evidence quality і ranking

Evidence quality — прозора евристика 0–100:

| Компонент | Вага |
| --- | ---: |
| Довжина історії | 25% |
| Trend signal та uncertainty | 30% |
| Абсолютний traffic base | 20% |
| Стійкість до peak month | 15% |
| Resolution confidence | 10% |

Для кількох series розраховується screening score із growth, absolute volume і reliability. Він підтримує shortlist, але не є launch threshold або автоматичним бізнес-рішенням.

Методологія повністю описана в [`methodology.md`](interest-signals/references/methodology.md).

## Артефакти й audit trail

Кожен повний run завжди створює:

- `request.json` — нормалізований request;
- `resolution.json` — mappings, confidence і warnings;
- `analysis.json` — observations, metrics, planner, findings і caveats;
- `manifest.json` — status, tool version, request digest, sources і список файлів.

За запитом також створюються:

- `series.csv`;
- `trend.svg`;
- `report.html`;
- `report.pdf`.

Артефакти спочатку записуються у staging directory. `manifest.json` публікується останнім як commit marker. Невдалий rerun не руйнує попередній завершений bundle.

PDF має односторінковий layout і перевіряє Unicode glyph coverage. Для тексту, який вимагає непідтриманого shaping або bidi layout, pipeline відмовляється створювати некоректний PDF і пропонує використати Unicode-safe JSON, CSV, SVG та HTML.

## Встановлення

Core pipeline використовує Python 3.11+ і стандартну бібліотеку. Pinned dependencies потрібні для PDF workflow:

```bash
cd interest-signals
python -m pip install -r requirements.lock.txt
```

## Приклад використання

Спочатку перевірити mapping:

```bash
cd interest-signals
python scripts/analyze.py \
  --topic "intermittent fasting" \
  --question "Which language has the most stable attention, and were there unusual spikes?" \
  --source-language en \
  --languages cs,de \
  --output-dir run-output \
  --resolve-only
```

Після перевірки `resolution.json` виконати повний аналіз:

```bash
python scripts/analyze.py \
  --topic "intermittent fasting" \
  --question "Which language has the most stable attention, and were there unusual spikes?" \
  --source-language en \
  --languages cs,de \
  --start 2024-01-01 \
  --end 2025-12-31 \
  --metric share \
  --formats csv,svg,html,pdf \
  --output-dir run-output
```

Для follow-up сценарію можна скопіювати `request.json`, змінити припущення й повторити run через `--config`. Той самий output directory повторно використовує локальний API cache; окремі output directories зберігають сценарії для side-by-side comparison.

## Evaluation framework

Evaluation-код зберігається поза runtime skill, щоб evaluated agent не бачив правильних відповідей. Framework має чотири рівні:

| Рівень | Що перевіряє | Мережа |
| --- | --- | --- |
| Engine | Метрики, uncertainty, gaps і robustness | Ні |
| Planner | Question-to-module routing і skip behavior | Ні |
| Agent | Читання analysis, grounded interpretation, trace та artifacts | Ні за замовчуванням |
| Live | Resolution, collection, cache і bundle integrity | Так, explicit opt-in |

Checked-in suites містять:

- 8 engine cases;
- 10 planner cases;
- 12 agent cases;
- 3 live cases.

Synthetic fixtures охоплюють smooth growth, flat history, spike, seasonality, level shift, volatility, short history, data gap, raw/share disagreement, low traffic, multi-series comparison і missing mapping.

### Запуск deterministic eval

```bash
python3 interest-signals-dev/evals/runner.py validate
python3 interest-signals-dev/evals/runner.py run-deterministic \
  --output interest-signals-dev/results/deterministic.json
```

Поточний checked-in baseline:

- 18/18 deterministic cases passed;
- planner exact match: `1.0`;
- planner module precision: `1.0`;
- planner module recall: `1.0`;
- planner module F1: `1.0`.

### Blind agent eval

```bash
python3 interest-signals-dev/evals/runner.py prepare-agent \
  --output interest-signals-dev/results/agent-prepared
```

Prepared workspace містить runtime skill, case prompt і synthetic input bundle, але не містить expected behavior, rubric або release thresholds. Manifest прив'язує grading до SHA-256 конкретного skill, prompt і evaluator-side case definition.

Після виконання agent cases:

```bash
python3 interest-signals-dev/evals/runner.py grade-agent \
  --prepared interest-signals-dev/results/agent-prepared \
  --output interest-signals-dev/results/agent-summary.json
```

Hard gates перевіряють `analysis_read`, відсутність market overclaim, коректне трактування missing data, module selection, requested artifacts, request digest і порядок resolution-first.

### Holdout і overfitting

Checked-in cases використовуються як regression suite. Для release evaluation передбачено приватний holdout із власними prompts і synthetic fixtures:

```bash
python3 interest-signals-dev/evals/runner.py run-deterministic \
  --case-root /secure/evals/holdout/cases \
  --output interest-signals-dev/results/holdout.json
```

Live tests відрізняють `product_failure` від `infrastructure_error`. Upstream data не використовується як єдиний correctness signal.

Повний eval protocol описаний у [`evaluation-and-roadmap.md`](interest-signals-dev/docs/evaluation-and-roadmap.md).

## Перевірка реалізації

```bash
ruff format --check interest-signals/scripts interest-signals-dev/evals interest-signals-dev/tests
ruff check interest-signals/scripts interest-signals-dev/evals interest-signals-dev/tests
PYTHONPATH=interest-signals/scripts \
  python3 -m unittest discover -s interest-signals-dev/tests -v
```

Поточний результат:

- 58 tests executed: 50 passed і 8 optional PDF tests skipped у середовищі без PDF/font dependencies;
- JSON schemas validated;
- isolated runtime package validated;
- skill frontmatter і структура validated.

Незалежний cheap-model forward test задокументовано в [`forward-test.md`](interest-signals-dev/docs/forward-test.md). Він перевірив baseline workflow та виявив реальний wording defect, після чого було додано regression test. Оскільки цей test передує adaptive planner v1.1, новий незалежний agent run потрібен перед остаточним release; без нього release status навмисно залишається `incomplete`.

## Поточні обмеження

- Є лише один source adapter.
- Один run аналізує одну тему в кількох мовах, а не portfolio topics × languages.
- Поточний page scope використовує current canonical title і не агрегує історію moves/redirects.
- Screening score — евристика, а не навчена decision model.
- Optional modules є descriptive diagnostics і не встановлюють причинність.
- Немає automatic event attribution.
- Немає multiple-comparison correction для великих portfolio runs.
- Follow-up requests можна повторювати через config і cache, але автоматичний scenario diff ще не реалізовано.
- Різні джерела не можна змішувати в один score без окремої calibration policy.

## Як ітеративно розвивати рішення для складніших досліджень і більших обсягів даних

Розвиток варто робити послідовними вертикальними кроками: кожна нова можливість має включати collection contract, provenance, caveats, synthetic fixtures, agent cases і live smoke test. Не слід одночасно розширювати джерела, статистику та ranking без окремої перевірки кожного шару.

### Етап 1. Завершити production-quality validation поточної версії

1. Повторити повний end-to-end test adaptive planner v1.1 на швидкій недорогій tool-capable моделі.
2. Перевірити skill activation без явного згадування його назви.
3. Зробити PDF dependencies обов'язковими в release environment і візуально перевірити кирилицю, довгі назви, gaps і 7+ series.
4. Додати offline record/replay transport, щоб agent eval міг сам запускати resolution і analysis без нестабільної мережі.
5. Зберігати точні prompts, traces, tool calls, latency, token usage і model configuration.

### Етап 2. Зробити follow-up дослідження першокласним workflow

1. Додати `compare-runs`, який порівнює два bundles і показує змінені assumptions, trends, rankings, warnings та modules.
2. Додати sensitivity analysis для ranking weights.
3. Показувати, як часто candidate залишається першим у допустимому просторі ваг.
4. Додати parent request digest, щоб кожен сценарій мав зв'язок із попереднім.
5. Генерувати компактний scenario-comparison report.

### Етап 3. Посилити concept і title history

1. Побудувати date-bounded ledger перейменувань сторінки.
2. Збирати views для історичних titles лише в період їхньої дії.
3. Додати reviewed redirect aliases без double counting.
4. Версіонувати правила concept aggregation.
5. Додати fixtures для moves, redirects, page merges і різних Wikidata mappings.

### Етап 4. Ввести source adapter registry

Спільний adapter contract має визначати:

```text
resolve_entities()
collect_observations()
normalize()
assess_quality()
build_provenance()
list_caveats()
```

Першим розширенням доцільно зробити generic CSV adapter. Він дозволить підключати exports із search-interest tools, Search Console, opt-in product analytics, ad platforms, surveys або внутрішніх систем без залежності від нестабільного зовнішнього API.

Наступні source adapters треба показувати окремими evidence panels. Об'єднувати їх у спільний score можна лише після calibration policy, яка визначає сумісність одиниць, population bias і missing-data semantics.

### Етап 5. Підтримати portfolio topics × languages

1. Розширити request schema до матриці тем і сегментів.
2. Додати batch resolution із повторним використанням project totals.
3. Ввести bounded concurrency не більше трьох upstream requests одночасно.
4. Додати resumable jobs, checkpoints і request-budget planning.
5. Зберігати великі observation tables у partitioned Parquet.
6. Використовувати DuckDB для локальної агрегації та filtering.
7. Застосувати false-discovery control для багатьох trends і anomaly scans.
8. Показувати Pareto frontier за growth, scale, reliability і user-supplied strategic fit.
9. Залишити в односторінковому PDF лише shortlist, а повні результати — у JSON, CSV або Parquet.

### Етап 6. Калібрувати статистичні методи

1. Побудувати simulation suite з seasonality, autocorrelation, structural breaks, missing periods, heavy-tailed noise і low counts.
2. Вимірювати interval coverage, false-positive rate, detection power і anomaly precision/recall.
3. Порівняти OLS із Theil–Sen, Huber regression та іншими robust estimators.
4. Додати uncertainty для seasonality і changepoint diagnostics.
5. Ввести minimum effect sizes і розділення exploratory та confirmatory analysis.
6. Не змінювати production thresholds, доки simulations і holdout eval не показують покращення.

### Етап 7. Масштабувати eval і release process

1. Запускати matrix із cheap, balanced і strong models.
2. Повторювати stochastic agent cases кілька разів і звітувати pass rate з uncertainty.
3. Додати activation precision/recall і near-miss prompts.
4. Додати property-based tests: gaps не стають нулями, optional modules не змінюють core, порядок input series не змінює scores.
5. Додати mutation testing, щоб переконатися, що evals ловлять навмисно внесені помилки.
6. Ротувати приватний holdout після витоку або тривалого використання.
7. Запускати невеликий scheduled live canary на стабільних historical ranges.
8. Відстежувати task success, hard-gate rate, latency, tokens, cost, tool calls, retries і artifact defects.

### Етап 8. Перейти від attention screening до decision validation

Після стабілізації multi-source architecture можна додати окремі evidence layers для:

- opt-in product analytics;
- search demand;
- survey та interview evidence;
- acquisition cost;
- conversion;
- retention;
- user-supplied strategic fit і localization cost.

Кожен layer має зберігати власну одиницю виміру, population bias і uncertainty. Фінальна рекомендація повинна показувати не один непрозорий score, а measured evidence, assumptions, conflicts між джерелами, sensitivity до критеріїв і конкретний наступний validation experiment.
