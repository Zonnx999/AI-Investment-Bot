# AI-Investment-Bot 코드 검토 가이드

> **목적**: 여러 검토자(AI/인간)가 각 섹션을 **독립적으로** 병렬 검토할 수 있도록
> 섹션별로 필요한 파일 목록, 컨텍스트, 체크리스트를 완전히 자급자족 형태로 작성했습니다.
>
> **규칙**:
> - 각 섹션은 서로 의존하지 않습니다. 담당 섹션만 읽고 검토 가능합니다.
> - 발견 내용은 각 체크박스 옆에 `[BUG]` / `[DESIGN]` / `[OK]` / `[TODO]` 중 하나로 태그하세요.
> - 버그라고 판단되면 라인 번호와 수정 방향을 메모해 두세요.
> - 작업 규칙(`CLAUDE.md §4`)을 준수합니다: 버그 즉시 수정 OK, 설계 결정은 옵션 제시 후 사용자 승인.

---

## 작업 배분표 (한눈에)

| # | 섹션 | 핵심 파일 | 담당 | 상태 |
|---|---|---|---|---|
| A | 점수 공식 일관성 | `screener.py`, `signals.py`, `universe.py` | | `[ ]` |
| B | 데이터 품질·빈 값 처리 | `data_fetcher.py`, `screener.py` | | `[ ]` |
| C | 로직 버그 후보 | `signals.py`, `universe.py` | | `[ ]` |
| D | 아키텍처·모듈 경계 | `bot.py`, `subscribers.py`, `bot_commands.py` | | `[ ]` |
| E | 운영 리스크 | `send_digest.py`, `build_universe.py`, `daily_update.py` | | `[ ]` |
| F | 보안·시크릿 | `http.py`, `storage.py`, `notifier.py` | | `[ ]` |
| G | 테스트 커버리지 | `tests/` 전체 | | `[ ]` |
| H | 성능·비용 | `signals.py`, `digest.py`, `storage.py` | | `[ ]` |
| I | 미확인 파일 | `predictors.py`, `logger.py`, `workflows/` | | `[ ]` |

---

## 섹션 A — 점수 공식 일관성

### 담당자에게
이 섹션은 종목 점수를 계산하는 세 경로가 서로 일관성이 있는지 확인합니다.
사용자에게는 "총점"이 하나처럼 보이지만, 코드 내부에서는 경로에 따라 공식이 다릅니다.

### 읽어야 할 파일 (전부 읽기)
```
src/screener.py          — health_scorecard, value_scorecard, screen_one
src/signals.py           — factor_scores, COMPOSITE_WEIGHTS, FactorScores dataclass
src/universe.py          — calculate_kr_scores, enrich, _compute_enrich
```

### 배경 지식
- **경로 1 (워치리스트 스크리너)**: `screener.screen_one()` → `health + value` 2팩터 단순 평균
- **경로 2 (팩터 신호 엔진)**: `signals.factor_scores()` → momentum/value/quality/low_vol 4팩터 각 0.25 가중
- **경로 3 (유니버스 DB, US)**: `universe._compute_enrich()` → `health_scorecard + value_scorecard` 후 `/2`
- **경로 4 (유니버스 DB, KR)**: `universe.calculate_kr_scores()` → 완전히 다른 공식 (ROE/부채비율/영업이익률/흑자보너스)

### 체크리스트

#### A-1. US 점수 경로 간 불일치
- [ ] `screener.screen_one()` (L267-304)의 `total = round((health + value) / 2)`과
      `signals.factor_scores()` (L266-296)의 composite 계산을 나란히 비교.
      **같은 티커에 대해 두 함수가 다른 total을 줄 수 있는가?** 의도적 설계인가?
- [ ] `universe._compute_enrich()` (L347-390)의 `total = round((health.total + value.total) / 2)` 역시
      `screen_one`과 동일 공식. 그렇다면 유니버스 DB 총점 ≠ 신호 엔진 복합점. 다이제스트에서
      두 점수가 사용자에게 혼용될 수 있는지 확인.

#### A-2. KR 점수 공식 검증 (`universe.py` L447-493)
- [ ] `health` 구성: ROE(max 35) + 부채비율(max 30) + 영업이익률(max 20) + 흑자보너스(15) = 100
      `Component("ROE", _clip((roe or 0) * 2.5, 0, 35), ...)` → ROE 14% 이상이면 만점. 합리적인가?
- [ ] `Component("부채비율", _clip(30.0 - (debt_ratio or 999) / 10, 0, 30), ...)` →
      debt_ratio=None 이면 `999/10=99.9` → `30-99.9=-69.9` → clip → **0점**. 데이터 없으면 0점인데
      vs US는 default=10.0 으로 중간값 처리. 정책 통일이 필요한가?
- [ ] `value` 구성 PBR 공식: `_clip(35.0 - pbr * 17.5, 0, 35)` →
      PBR 2.0 → `35 - 35 = 0점`, PBR 1.0 → `17.5점`, PBR 0.1 → `33.25점`.
      **PBR 2.0 = 0점**이 한국 중대형주 현실에서 맞는가? 삼성전자 PBR이 약 1.3인데 이 공식에서 몇 점인지 계산.

#### A-3. `_clip` 함수 중복
- [ ] `universe.py` L465-466에 `def _clip(v, lo, hi):` 로컬 정의 존재.
      `screener.py`에도 `_clip`이 있음 (L147-148). 동일 로직 중복. 제거 가능한가?

#### A-4. KR 배당수익률 누락
- [ ] `universe._discover_kr()` (L229-262)에서 `dividend_yield=0.0` 하드코딩.
      주석에 "배당은 DART(9b)" 라고 되어 있지만, `enrich_kr()` (L507-576)에서 `dividend_yield`를
      업데이트하는 코드가 없다. KR 종목은 배당수익률이 영원히 0%로 남는가? 의도된 미구현인가?

#### A-5. 발견 요약
| 항목 | 판정 | 메모 |
|---|---|---|
| US 경로별 총점 불일치 | [DESIGN] | 경로1(`screen_one` L286)·경로3(`_compute_enrich` L375)는 `round((health+value)/2)` 동일. 경로2(`factor_scores` L290-295)는 모멘텀·밸류·퀄리티·로우볼 각 0.25 가중 → 의도적 설계 차이(신호 엔진 vs 스크리너). 버그 아님. 다만 다이제스트에서 두 총점이 같은 화면에 혼용되면 사용자 혼란 발생 가능 — 표시 문맥 명확화 권장 |
| KR PBR 공식 적정성 | [DESIGN] | `35.0 - pbr*17.5`(L483): PBR=2.0→0점, 삼성전자(PBR≈1.3)→12.25/35(35%). US 공식(`15-pbr*1.5`)은 PBR=2.0→12/15(80%)로 훨씬 관대. 한국 중대형주 PBR 실분포(1.0~2.5)를 감안하면 PBR 컷오프 2.0이 너무 가혹 — 기준치 완화 검토 권장 |
| `_clip` 중복 | [DESIGN] | `screener.py` L147과 `universe.calculate_kr_scores` L465에 동일 2줄 로직 중복. `utils.py`로 이동하거나 `screener._clip`을 직접 import하여 해소 가능. 임팩트 낮음 |
| KR 배당 0 고정 | [TODO] | `_discover_kr` L259 `dividend_yield=0.0` 하드코딩 + `enrich_kr`(L539-559) 업데이트 없음 — 주석에 "배당은 DART(9b)"로 의도적 미구현 명시. 추가로 `calculate_kr_scores` value 공식(L478-485)에 배당 컴포넌트 자체가 없음(US value_scorecard는 배당수익률 10점 포함). Phase 9b DART 배당 데이터 연동 시 공식도 함께 수정 필요 |

---

## 섹션 B — 데이터 품질·빈 값 처리

### 담당자에게
외부 API 응답의 예상치 못한 형태(문자열 대신 null, dict 대신 list 등)가
점수 계산이나 DB 저장에서 조용히 틀린 결과를 내거나 크래시를 일으킬 수 있습니다.

### 읽어야 할 파일
```
src/data_fetcher.py      — _fmp_get, _fmp_to_dataframe, fetch_company_screener,
                           fetch_quote, _parse_dart_accounts
src/screener.py          — _safe, latest_fundamentals
```

### 배경 지식
- FMP는 에러를 두 가지 방식으로 줌: (1) 4xx/5xx HTTP 코드, (2) HTTP 200 + `{"Error Message": "..."}` dict
- DART `fnlttSinglAcnt` 응답에서 계정명은 한글 정확 매칭 필요
- `_safe(d, key, default=0.0)`: `float(default if v is None else v)` — None 체크만 함

### 체크리스트

#### B-1. `_safe()` 비 float 값 처리 (`screener.py` L141-144)
```python
def _safe(d: dict, key: str, default: float = 0.0) -> float:
    v = d.get(key)
    return float(default if v is None else v)
```
- [ ] FMP가 `"N/A"`, `"–"`, `""` 같은 문자열을 숫자 필드에 반환하면 `float("N/A")` → `ValueError` 발생.
      catch 없음. 실제 FMP 응답에서 이런 케이스가 있었던 적 있는가? `diag_fmp.py`로 확인 가능.
- [ ] `v`가 `True/False`(bool)이면 `float(True)==1.0` — 의도치 않은 1점 가산 가능성.
      FMP 응답에 bool 필드가 숫자 필드와 같은 이름으로 올 수 있는 엔드포인트가 있는가?

#### B-2. `fetch_company_screener()` 에러 dict 방어 (`data_fetcher.py` L499-528)
```python
data = _fmp_get("company-screener", params)
if not data:
    return []
if exclude_funds:
    data = [d for d in data if not (d.get("isEtf") or d.get("isFund"))]
return data
```
- [ ] FMP가 HTTP 200 + `{"Error Message": "Invalid API key"}` dict를 반환하면:
      `if not data` — dict는 truthy → 통과.
      `[d for d in data if ...]` — dict 이터레이션은 key 문자열을 냄.
      `d.get("isEtf")` → `str`에 `.get()` 없음 → `AttributeError` 크래시.
      실제로 `_fmp_get`이 이 케이스를 막아주는지 확인. `_fmp_get`은 4xx/5xx만 처리함.
- [ ] `_fmp_to_dataframe()` (L438-453)에는 `not isinstance(data, list): return pd.DataFrame()` 가드가 있지만
      `fetch_company_screener`는 `_fmp_to_dataframe`을 쓰지 않음. 동일 가드를 추가해야 하는가?

#### B-3. `_parse_dart_accounts()` 계정명 정확 매칭 (`data_fetcher.py` L725-754)
```python
"net_income": base.get("당기순이익(손실)"),
"equity":     base.get("자본총계"),
```
- [ ] 흑자 기업은 DART에서 `"당기순이익"` (괄호 없이)을 쓸 수도 있다.
      `base.get("당기순이익(손실)")` → None → `roe=None, per=None` → 점수 0.
      `test_dart.py`에서 이 케이스를 테스트하는지 확인.
- [ ] `"매출액"` 계정명도 `"수익(매출액)"` 등으로 변형이 있을 수 있음. 실제 DART API 응답 샘플로 검증.
- [ ] CFS 없을 때 OFS 폴백: `base = cfs or ofs` → `cfs={}`이면 falsy라 OFS로 가는가?
      `cfs`가 빈 dict `{}`이면 `bool({}) == False` → OFS로 폴백. 의도인가? 실제 CFS 데이터가 있는데
      모든 계정명이 매칭 안 되어 `cfs={}`가 되는 경우?

#### B-4. `latest_fundamentals()` 빈 응답 (`screener.py` L76-90)
```python
merged: dict = {}
km = fetch_key_metrics(ticker, limit=1)
if not km.empty:
    merged.update(km.iloc[-1].to_dict())
rt = fetch_ratios(ticker, limit=1)
if not rt.empty:
    for k, v in rt.iloc[-1].to_dict().items():
        merged.setdefault(k, v)
return merged
```
- [ ] 둘 다 empty면 `{}` 반환 → `health_scorecard({})` / `value_scorecard({}, {})` →
      모든 `_safe` 호출이 default 반환 → **데이터 누락이 0점으로 조용히 처리됨**.
      호출부(`factor_scores`, `screen_one`)에서 이 케이스를 어떻게 처리하는지 확인.
- [ ] `km.iloc[-1].to_dict()`에서 NaN 값들이 `None`이 아닌 `float('nan')`으로 들어올 수 있음.
      `_safe(d, key, 0.0)` → `v = float('nan')` → `float(nan)` → NaN 반환. 점수 계산에서 NaN
      전파 가능성. `math.isnan` 체크가 없음.

#### B-5. 발견 요약
| 항목 | 판정 | 메모 |
|---|---|---|
| `_safe()` 문자열 ValueError | [BUG] | `float("N/A")` → ValueError. `_safe`에 try/except 없음. 수정: `try: return float(v) except (ValueError, TypeError): return float(default)` |
| screener 200+에러dict 방어 부재 | [BUG] | `_fmp_get` L435가 `{"Error Message": ...}` dict를 통과. `fetch_company_screener` L526에서 dict 이터레이션(key 문자열) → `str.get()` → `AttributeError` 크래시. 수정: `if not isinstance(data, list): return []` 가드 추가 |
| DART 계정명 정확 매칭 리스크 | [BUG] | `base.get("당기순이익(손실)")` 정확 매칭만. 흑자 기업에서 `"당기순이익"` (괄호 없음) 사용 시 None → net_income=None → ROE/PER 0점. 수정: `or base.get("당기순이익")` 폴백 추가 |
| 빈 fundamentals = 조용한 0점 | [DESIGN] | 두 DataFrame 모두 empty면 `{}` 반환(예외 없음) → 기본값 점수(total≈5)로 결과 포함. 데이터 누락과 실제 저점 구분 불가. 대안: empty 시 호출부 skip 처리 |
| NaN 전파 가능성 | [BUG] | pandas NaN → `_safe` 통과 → `_clip(nan, 0, hi)`에서 `min(hi, nan)=hi` → 해당 컴포넌트 만점 부여. `sum(...nan...)=nan` → `min(100.0, nan)=100.0` → ScoreCard 100점. 데이터 없는 항목이 만점 기여로 총점 과대계상. 수정: `_safe`에 `math.isnan` 체크 추가 |

---

## 섹션 C — 로직 버그 후보

### 담당자에게
코드가 실행은 되지만 의도와 다른 결과를 내는 로직 버그를 찾습니다.
각 항목에서 "이 코드가 실제로 이 케이스에서 어떻게 동작하는가"를 손으로 추적하세요.

### 읽어야 할 파일
```
src/signals.py           — generate_signal_report, drawdown_alerts (L337-404)
src/universe.py          — _discover_kr (L229-263), enrich_kr (L507-576),
                           calculate_kr_scores (L447-493)
```

### 체크리스트

#### C-1. `drawdown_alerts()` 이중 호출 (`signals.py` L374-386)
```python
# 낙폭 breach 상태는 알림 억제와 무관하게 항상 계산 → 상태 저장에 사용.
_, dd_state = drawdown_alerts(dd, prev.get("dd_breached", {}))
alerts: list[Alert] = []
if not first_run:
    ...
    dd_alerts, _ = drawdown_alerts(dd, prev.get("dd_breached", {}))
    alerts.extend(dd_alerts)
```
- [ ] 동일 인자로 `drawdown_alerts`가 두 번 호출됨. 순수 함수이므로 결과는 동일.
      첫 호출의 `_`(버려진 alerts)와 두 번째 호출의 `dd_alerts`는 같은 값.
      첫 호출의 `dd_state`와 두 번째 호출의 `_`도 같은 값.
      **실제 버그는 없으나** 코드가 의도를 명확히 표현하지 않음. 리팩토링 필요성 평가.
- [ ] `first_run=True`일 때 `dd_state`(첫 호출 결과)는 state에 저장됨 (L390-394). 올바른가?
      첫 실행에서 breach 상태를 시딩하는 것이 맞는 설계인지 확인.

#### C-2. `_discover_kr()` KOSDAQ 실패 미처리 (`universe.py` L229-263)
```python
def _discover_kr(conn, floor=KR_MARKET_CAP_FLOOR) -> int:
    bas_dd, kospi = _latest_kr_trading_day()
    if not bas_dd:
        return 0
    kosdaq = fetch_krx_daily("KOSDAQ", bas_dd)   # ← DataFetchError 발생 시 catch 없음
    common = _common_stock_codes(bas_dd)
```
- [ ] `fetch_krx_daily("KOSDAQ", bas_dd)`가 `DataFetchError`를 던지면 `_discover_kr`을 호출한
      `discover()` 전체로 예외가 전파됨. `discover()`의 `elif mkt == "KR":` 블록에도 catch 없음.
      → **KOSDAQ API 실패 시 US/CRYPTO 발굴까지 중단될 수 있음**.
      `_latest_kr_trading_day()` 내의 KOSPI 실패는 `(None, [])` 반환으로 우아하게 처리하는데
      KOSDAQ은 왜 다른가?
- [ ] `_common_stock_codes(bas_dd)` 내부의 `fetch_krx_base_info()` 실패도 catch 없음.
      KOSPI/KOSDAQ 기본정보 API가 일별매매 API와 별개로 실패할 수 있는가?

#### C-3. `enrich_kr()` `equity=0` 처리 (`universe.py` L549-552)
```python
if fin and fin.get("equity") and mcap:
    sc = calculate_kr_scores(fin, float(mcap[0]))
```
- [ ] `fin.get("equity") == 0.0` (자본총계가 실제 0원인 완전 자본잠식 기업)이면 `bool(0.0) == False`
      → `stats["no_data"]` 처리. 데이터 누락과 자본잠식이 같은 통로로 처리됨.
      자본잠식 기업을 발굴 대상에서 제외하는 것은 맞지만, 로그에 구분이 없어 분석이 어렵다.
- [ ] `fin.get("net_income") == 0.0`인 경우(수지균형): `per = (mcap / 0.0)` → ZeroDivisionError?
      `calculate_kr_scores()` L456: `per = (market_cap / ni) if (ni and ni > 0) else None` →
      `ni=0.0`이면 `bool(0.0)==False` → `per=None`. 안전함. 맞는가?

#### C-4. EarningsYield → P/E 변환 경계값 (`signals.py` L310-320)
```python
ey = pick_first(m, ["earningsYield"])
rows.append({
    "ticker": t,
    "pe": (1.0 / ey) if ey else None,
    ...
})
```
- [ ] `ey`가 아주 작은 양수(예: 0.0001)이면 `pe = 10000`. `pe_median`을 거의 확실히 초과하므로
      스크리닝에서 자동 탈락. 의도대로인가?
- [ ] `ey`가 음수(적자 기업)이면 `bool(ey) == True` (0이 아닌 수) → `pe = 1/ey < 0`.
      `valid_pes = [r["pe"] for r in rows if r.get("pe") and r["pe"] > 0]` 에서 `pe<0`이면 제외.
      하지만 `apply_screen_rules()` L196: `if pe and pe > 0 and pe_median is not None:` → 음수 PE는
      P/E 룰을 면제받고, ROE/FCF yield 룰만 통과하면 스크리닝 통과 가능. **적자 기업이 스크리닝 통과 가능한가?**

#### C-5. 발견 요약
| 항목 | 판정 | 메모 |
|---|---|---|
| `drawdown_alerts` 이중 호출 | [DESIGN] | 순수 함수 동일 인자로 2회 호출 — 결과 동일, 버그 없음. `dd_alerts, dd_state = drawdown_alerts(...)` 단일 호출 후 first_run 분기로 리팩토링 가능. |
| KOSDAQ 실패 전파 | [BUG] | `_discover_kr` L240 KOSDAQ 호출에 try/except 없음 → DataFetchError가 `discover()` KR 블록으로 전파 → 루프 중단 → CRYPTO 발굴 미실행. US는 L276-282에 catch 있음. 수정: `_discover_kr` 내 KOSDAQ 호출을 try/except로 감싸 KOSPI만으로 폴백 (KOSPI 실패가 `(None, [])` 반환하는 것과 일관성 맞춤). |
| `equity=0` 처리 | [DESIGN] | 완전 자본잠식(`equity=0.0`) → `bool(0.0)==False` → 데이터 누락과 동일 `no_data` 카운터. 기능 오류 없음(자본잠식 제외 의도 부합). 로그에 구분 없어 사후 분석 어려움. |
| 음수 P/E 스크리닝 통과 | [DESIGN] | `ey<0` → `pe<0` → `pe>0` 조건 실패 → P/E 룰 면제. ROE≤10% 필터(L183)가 실질 차단. 일시 손실 기업(음수 EY + 양수 역사적 ROE + 양수 FCF) 이론상 통과 가능. 수정 방향: `ey<0`이면 `pe=None`으로 명시 처리. |

---

## 섹션 D — 아키텍처·모듈 경계

### 담당자에게
모듈 간 의존성과 캡슐화 위반을 찾습니다. `CLAUDE.md §4.4`의 모듈 책임 분리 원칙과
실제 코드가 얼마나 일치하는지 확인합니다.

### 읽어야 할 파일
```
scripts/bot.py            — run() 전체 (L57-115)
src/subscribers.py        — _OFFSET_NS, _OFFSET_KEY, _conn (L41-44, L70-75)
src/bot_commands.py       — BUTTON_TO_COMMAND, main_keyboard, respond (L60-228)
src/digest.py             — _MARKET_LABEL (L28)
```

### 체크리스트

#### D-1. Private 심볼 외부 접근 (`bot.py`)
```python
# bot.py L62-63
offset = store.get_state(subscribers._OFFSET_NS, subscribers._OFFSET_KEY)
...
# bot.py L54
return subscribers.get_status(subscribers._conn(), chat_id) == "active"
```
- [ ] `subscribers._conn`, `subscribers._OFFSET_NS`, `subscribers._OFFSET_KEY` 모두 private(`_` prefix).
      `bot.py`가 이들을 직접 접근함. `subscribers.py`가 수정되면 `bot.py`도 함께 깨진다.
      이들을 public API로 올리는 것이 낫지 않은가? (`OFFSET_NS`, `OFFSET_KEY`, `get_conn()`)
- [ ] `_is_subscriber()` 내에서 `subscribers._conn()` 호출 → 매 메시지마다 `conn.executescript(_SCHEMA)` 실행.
      `executescript`의 `CREATE TABLE IF NOT EXISTS`는 멱등이지만 Turso 환경에서 불필요한 왕복.
      대안: 봇 시작 시 한 번 `_conn()` 호출 후 재사용.

#### D-2. `run()` 반환 타입 불일치 (`bot.py` L57, L117-125)
```python
def run() -> int:          # 타입 힌트는 int
    ...
    while True:
        ...
    # return 문 없음 → None 반환
```
- [ ] `run()`은 `while True` 루프로 `KeyboardInterrupt`나 예외 없이는 절대 반환하지 않음.
      `main()`에서 `return run()` → `return None`. 타입 힌트 `-> int`와 불일치.
      `main()`의 반환값이 `raise SystemExit(main())`으로 쓰이므로 `SystemExit(None)`이 됨.
      `SystemExit(None)`은 OS에 종료 코드 0을 반환하므로 기능적 문제는 없지만 타입 오류.

#### D-3. 레이블 중복 정의
```python
# digest.py L28
_MARKET_LABEL = {"KR": "🇰🇷 한국", "US": "🇺🇸 미국"}

# bot_commands.py L93-94
_MKT_FLAG  = {"US": "🇺🇸", "KR": "🇰🇷", "CRYPTO": "🪙"}
_MKT_LABEL = {"US": "🇺🇸 미국", "KR": "🇰🇷 한국", "CRYPTO": "🪙 크립토"}
```
- [ ] `_MARKET_LABEL`이 두 파일에 따로 정의됨. `utils.py`에 통합하는 것이 적절한가?
      CLAUDE.md §4.4: "2곳 이상에서 반복되는 순수 헬퍼만 utils.py에."

#### D-4. 업데이트 처리 순서 안전성 (`bot.py` L77-114)
```python
# 0) 버튼 정규화 (모든 updates)
for u in updates:
    msg["text"] = BUTTON_TO_COMMAND.get(msg["text"], msg["text"])

# 1) 조회 명령 처리 (모든 updates)
for u in updates:
    ...
    reply = bot_commands.respond(text, chat_id, limiter)

# 2) 구독 명령 처리 (모든 updates)
events, next_offset = subscribers.parse_updates(updates)
subscribers.apply_events(events)
```
- [ ] `/start` 명령: 루프1 → `parse_command("/start")` = `unknown` → `respond()` = None (아무것도 안 함).
      루프2 → `_classify("/start")` = `"request"` → `apply_events`에서 처리. **정상인가?**
- [ ] "📋 구독자" 버튼: 루프0 → `"/subscribers"`로 변환. 루프1 → `parse_command("/subscribers")` = `unknown` → None.
      루프2 → `_classify("/subscribers")` = `"list"` → `apply_events`에서 소유자면 처리. **정상인가?**
- [ ] 같은 update가 루프1과 루프2 모두에서 처리될 수 있는가? 예: `/stock AAPL` →
      루프1: `respond()` 반환 후 처리.
      루프2: `_classify("/stock AAPL")` = `"ignore"` → `SubEvent(kind="ignore")` → `apply_events` 무시.
      이중 처리 없음. **확인 필요.**

#### D-5. 발견 요약
| 항목 | 판정 | 메모 |
|---|---|---|
| private 심볼 외부 접근 | [DESIGN] | `bot.py` L54·62·113에서 `subscribers._conn`, `_OFFSET_NS`, `_OFFSET_KEY` 직접 접근 — public API 없음. 더 큰 문제: `_is_subscriber()`가 매 메시지마다 `_conn()` 호출 → `executescript(_SCHEMA)`(`CREATE TABLE IF NOT EXISTS`) + `add_column_if_missing`(`PRAGMA table_info`) 반복 실행, 불필요한 Turso 왕복. 봇 시작 시 한 번 `_conn()` 호출 후 커넥션 재사용 권장 |
| `run()` 반환 타입 불일치 | [DESIGN] | `run() -> int`(L57)이나 `while True` 루프라 정상 반환 없음 → 실제 타입은 `NoReturn`. `main()`의 `return run()`은 예외 없이 None 반환 → `SystemExit(None)` → OS 종료 코드 0 (기능 문제 없음). `run() -> NoReturn`으로 수정하면 mypy 경고 해소 |
| 레이블 중복 정의 | [DESIGN] | `digest._MARKET_LABEL`(L28: `{"KR":..., "US":...}`)과 `bot_commands._MKT_LABEL`(L94: `{"US":..., "KR":..., "CRYPTO":...}`) 유사 dict 두 곳 존재. CRYPTO 포함 여부만 다름. `utils.py`에 CRYPTO 포함 단일 dict로 통합 가능 (CLAUDE.md §4.4 "2곳 이상 반복 순수 헬퍼") |
| 업데이트 이중 처리 안전성 | [OK] | 3-루프 설계 올바름. 구독 명령(`/start`, `/subscribers` 등)은 Loop 1에서 `Command("unknown")` → `respond()` None → 무시, Loop 2에서만 처리. 조회 명령(`/stock`, `/scan`)은 Loop 1 처리 후 Loop 2에서 `kind="ignore"` → `apply_events` if/elif 미해당 → 무시. 버튼 클릭은 Loop 0 정규화 후 동일 흐름. 이중 처리 없음 확인 |

---

## 섹션 E — 운영 리스크

### 담당자에게
실제 배포 환경에서 cron, 상시 봇, 유니버스 빌드가 동시에 돌 때 발생할 수 있는
운영 리스크를 확인합니다. DEPLOYMENT.md도 함께 읽으세요.

### 읽어야 할 파일
```
scripts/send_digest.py    — 전체 (L1-67)
scripts/build_universe.py — 전체 (L1-119)
scripts/daily_update.py   — 전체 (전부 읽기)
docs/DEPLOYMENT.md        — systemd 서비스 설정 확인
```

### 체크리스트

#### E-1. offset 경합 리스크
- [ ] `DEPLOYMENT.md`에서 `quant-bot` 서비스(상시 폴링)와 cron `send_digest.py`가 동시에 실행될 때
      `--no-sync` 플래그가 자동으로 전달되는지 확인.
      cron 명령 라인에 `--no-sync`가 빠지면 두 프로세스가 같은 offset을 소비하며
      일부 구독 명령이 유실됨.
- [ ] `send_digest.py`에 `--no-sync` 없이 실행하면 `sync_subscribers()` → `get_updates(offset)` 호출.
      이때 봇이 이미 같은 offset을 처리했다면 텔레그램이 `[]`를 반환하므로 기능적 문제는 없다.
      하지만 봇이 offset을 미처 전진시키기 전에 cron이 실행되면 중복 처리 발생 가능성 있음.

#### E-2. `build_universe.py` private 함수 접근 (L93-94)
```python
kr_pending = len(universe._kr_symbols_needing_enrichment(max_age))
```
- [ ] `universe._kr_symbols_needing_enrichment` (private, `_` prefix)를 스크립트에서 직접 호출.
      `symbols_needing_enrichment` (public)은 US만 반환하므로 KR 카운트를 위해 private을 씀.
      `universe.stats()`로 `KR_enriched`를 파악하는 방법은 충분하지 않은가?

#### E-3. `--force` 옵션 동작 검증 (`build_universe.py` L67)
```python
max_age = timedelta(0) if args.force else timedelta(days=args.max_age)
```
- [ ] `timedelta(0)` = 0초. `symbols_needing_enrichment(max_age)` 내 cutoff:
      `cutoff = (now - timedelta(0)).isoformat() = now.isoformat()`.
      조건: `updated_at < now` → 과거에 업데이트된 모든 행이 해당. 논리적으로 맞음.
      단, 이 스크립트가 실행되는 **동안** 업데이트된 행도 재보강 대상이 될 수 있는가?
      (극히 빠른 flush와 다음 쿼리 사이의 레이스 컨디션 가능성)

#### E-4. `daily_update.py` 흐름 확인
- [ ] `daily_update.py`가 어떤 순서로 어떤 함수를 호출하는지 직접 읽어서 확인.
      오케스트레이터로서 실패 처리(개별 모듈 실패가 전체를 막는가?)를 확인.
- [ ] cron에서 `daily_update.py`와 `send_digest.py`가 어떤 순서로 실행되는가?
      `daily_update`가 실패해도 `send_digest`가 실행되는가?

#### E-5. 발견 요약
| 항목 | 판정 | 메모 |
|---|---|---|
| offset 경합 (--no-sync) | [OK] | `quant-digest@.service` ExecStart에 `--no-sync` 하드코딩 확인(DEPLOYMENT.md §5.1). 전용 레플리카(`digest_replica.db`)도 설정. GitHub Actions 스케줄 비활성. 단, 실수로 GH Actions 재활성화 시 `--no-sync` 없이 실행 → 경합 발생 가능 — 잠재 위험 |
| private API 직접 접근 | [DESIGN] | `build_universe.py` L93: `universe._kr_symbols_needing_enrichment()` 직접 호출. 공개 `symbols_needing_enrichment`가 US 전용이라 우회. `stats()`에 KR pending 카운터 없음. 수정 방향: `symbols_needing_enrichment(max_age, market)` 파라미터 확장 |
| --force 레이스 컨디션 | [OK] | 단일 프로세스에서 레이스 없음. L83에서 대상 목록 선취 후 보강. L110 remaining이 --force 후 0이 아닐 수 있음(max_age=0 → 방금 보강한 행도 updated_at < 새 now 해당)이나 표시 이슈일 뿐, 보강 로직 자체는 정상 |
| daily_update 실패 처리 | [OK] | 각 섹션 `try/except QuantBotError + Exception(noqa BLE001)` 완전 격리. 실패 시 exit code 1. L166 `sync()` 명시적 호출. DEPLOYMENT.md에 `daily_update.py` timer 없음 — 현재 배포에서 수동 실행 전용. `send_digest.py`가 신호·상태 직접 갱신하므로 순서 의존 없음 |

---

## 섹션 F — 보안·시크릿

### 담당자에게
API 키 노출, SQL 인젝션 가능성, Markdown 인젝션 등 보안 이슈를 확인합니다.
CLAUDE.md §4.9 보안 규칙을 기준으로 삼으세요.

### 읽어야 할 파일
```
src/http.py              — SecretMaskingFilter, mask_secrets, install_secret_masking
src/storage.py           — add_column_if_missing (L300-316)
src/notifier.py          — send_telegram, Markdown 파싱 실패 폴백 (L79-117)
src/config.py            — Settings.require (L49-61)
```

### 체크리스트

#### F-1. `add_column_if_missing()` SQL 인젝션 (`storage.py` L300-316)
```python
def add_column_if_missing(conn, table: str, column: str, coltype: str) -> None:
    existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column in existing:
        return
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
```
- [ ] `table`, `column`, `coltype` 모두 f-string으로 SQL에 인라인됨. 파라미터 바인딩 없음.
      현재 호출부: `add_column_if_missing(conn, "screened", "per", "REAL")` 등 전부 리터럴 호출.
      외부 입력(사용자 입력, API 응답)이 이 함수에 전달될 경로가 있는가? 지금은 없어 보이지만
      미래 확장 시 위험. 주석으로라도 "호출부는 항상 리터럴 상수를 사용할 것" 명시가 필요한가?

#### F-2. `SecretMaskingFilter` 커버리지 한계 (`http.py` L95-128)
- [ ] `install_secret_masking()` (L124-129)은 `logging` 경로의 record에만 마스킹 적용.
      Python 기본 crash (`sys.excepthook`)가 stderr에 직접 출력하는 traceback은 마스킹 없음.
      `scripts/bot.py`의 `while True` 루프: `except Exception: logger.exception(...)` 으로 처리하므로
      logging 경로를 탐. 하지만 루프 **밖**에서 `MissingApiKeyError`가 잡히는 `main()`에서는?
      `settings.require("telegram_bot_token")` 실패 시 에러 메시지에 실제 키가 들어가는가?
- [ ] `SecretMaskingFilter`는 `root` 로거의 핸들러에 붙음 (import 시점). 하지만 `install_secret_masking()`
      이 호출되는 시점에 핸들러가 아직 추가되지 않았다면? → `root.handlers`가 비어있어 아무 핸들러에도
      필터가 붙지 않음. `http.py`가 `logger.py`보다 먼저 import되는 시나리오 확인.

#### F-3. `Settings.require()` 키 이름 실수 (`config.py` L49-61)
```python
def require(self, key: str) -> str:
    value = getattr(self, key, "")
    if not value:
        raise MissingApiKeyError(key_name=key.upper())
    return value
```
- [ ] `require("FRED_API_KEY")` (대문자로 잘못 호출 시): `getattr(self, "FRED_API_KEY", "")` → `""`
      (해당 속성 없음) → `MissingApiKeyError("FRED_API_KEY")` 발생. 에러 메시지는 맞지만
      실제로 키가 설정되어 있어도 오류가 남. 모든 호출부가 `"fred_api_key"` 소문자로 올바르게 호출하는지 grep으로 확인.
- [ ] `MissingApiKeyError`의 기본 메시지: ".env에 {key_name}가 설정되어 있지 않습니다."
      `key_name=key.upper()` 이므로 메시지에 환경변수 이름이 올바르게 표시됨. OK.

#### F-4. Markdown 파싱 실패 감지 (`notifier.py` L105-115)
```python
except ApiHttpError as e:
    if parse_mode and "parse" in str(e).lower():
        logger.warning("Markdown 파싱 실패 — 평문으로 재전송")
        result = _post(None)
    else:
        raise
```
- [ ] 텔레그램 에러 메시지에 "parse"가 포함되지 않을 수 있음. 실제 텔레그램 API의
      파싱 실패 에러 메시지 포맷을 확인: `"Bad Request: can't parse entities in message text"`.
      "parse" 포함 → 현재 코드로 감지됨. 하지만 텔레그램이 에러 메시지를 바꾸면 폴백 없이 실패.
      더 안정적인 방법: `response.status_code == 400`과 Markdown-specific 에러 코드 확인.

#### F-5. 발견 요약
| 항목 | 판정 | 메모 |
|---|---|---|
| SQL f-string (로우 리스크) | [DESIGN] | SQLite DDL은 식별자 파라미터 바인딩이 구조상 불가(SQL 표준 제약). 현재 모든 호출부가 리터럴 상수라 실질 인젝션 경로 없음. docstring에 "호출부는 항상 리터럴 상수만 전달" 명시 권장. |
| SecretMaskingFilter import 순서 | [OK] | `http.py` L48의 `logger = get_logger(__name__)`가 `setup_logging()`을 선행 실행 → root 핸들러 생성 완료 후 L205의 `install_secret_masking()`이 필터 부착. 타이밍 문제 없음. `sys.excepthook` raw crash는 logging 우회 → 마스킹 불가 — CLAUDE.md §4.9 인지된 한계. |
| `require()` 대소문자 키 오류 | [OK] | 전체 grep 결과 모든 호출부(7곳) 소문자 키 사용 확인(`fred_api_key`, `fmp_api_key`, `dart_api_key` 등). 잘못된 키명은 `getattr` → `""` → `MissingApiKeyError` 즉시 발생(묵시 실패 없음). |
| Markdown 폴백 감지 취약성 | [DESIGN] | `"parse" in str(e).lower()` 로 파싱 실패 감지. 현재 텔레그램 응답(`"can't parse entities in message text"`)에 "parse" 포함 → OK. 텔레그램 에러 메시지 변경 시 폴백 미작동 → `ApiHttpError` 재발생. 보완: `e.status_code == 400 and parse_mode is not None` 조건 대체 고려. |

---

## 섹션 G — 테스트 커버리지

### 담당자에게
현재 140개 테스트가 통과되지만, 실제로 중요한 케이스가 커버되고 있는지 확인합니다.
`python -m pytest --tb=no -q`로 실행 후 각 파일을 직접 읽어 판단하세요.

### 읽어야 할 파일
```
tests/test_screener_scoring.py
tests/test_universe.py
tests/test_signals.py
tests/test_data_fetcher.py
tests/test_dart.py
tests/test_bot_commands.py
tests/conftest.py
```

### 체크리스트

#### G-1. 핵심 케이스 누락 확인

- [ ] **`calculate_kr_scores()` 극단값**: `test_universe.py`에서 `equity=None`, `equity=0.0`,
      `net_income` 음수, `market_cap=0` 케이스가 테스트되는가?
- [ ] **`_sql_lit()` 엣지케이스** (`universe.py` L121-134):
      `float("inf")`, `float("nan")`, `True/False`, numpy 스칼라, 아포스트로피 포함 한국어 문자열.
      `test_universe.py`에서 이 함수를 직접 테스트하는가?
- [ ] **`_parse_dart_accounts()`** (`test_dart.py`):
      CFS 없이 OFS만 있는 케이스, 둘 다 없는 케이스, "당기순이익(손실)" vs "당기순이익" 케이스가 있는가?
- [ ] **`bot.py run()` 루프 로직**: 버튼 정규화 → 구독 게이트 → rate limit 순서가 통합 테스트로 있는가?
      아니면 단위 테스트만 있는가?
- [ ] **`test_data_fetcher.py`** (42줄): `fetch_company_screener`, `_fmp_get`, `fetch_quote`의
      에러 케이스(200+에러dict, 빈 응답)가 테스트되는가?

#### G-2. `conftest.py` 확인
- [ ] `conftest.py` (73줄)의 fixture 목록: 어떤 공통 픽스처가 있는가?
      `tmp_path` 기반 SQLite DB 픽스처가 있어서 universe/storage 테스트가 메모리/임시 DB로 돌아가는지 확인.

#### G-3. 테스트 커버리지 미달 목록 작성
아래 표를 채워주세요:

| 함수/케이스 | 파일 | 테스트 있음? | 우선순위 |
|---|---|---|---|
| `calculate_kr_scores(equity=None)` | `universe.py` | ✅ 있음 (`test_dart.py::test_kr_scores_missing_data_safe`) | 높음 |
| `calculate_kr_scores(net_income<0)` | `universe.py` | ✅ 있음 (`test_dart.py::test_kr_scores_loss_maker_no_per`) | 높음 |
| `_sql_lit(float("nan"))` | `universe.py` | ✅ 있음 (`test_universe.py::test_sql_lit_escaping`) | 높음 |
| `_sql_lit("O'Brien")` | `universe.py` | ✅ 있음 (`test_universe.py::test_sql_lit_escaping`) | 높음 |
| `fetch_company_screener` 에러 dict 처리 | `data_fetcher.py` | ❌ 없음 (`_fmp_to_dataframe`의 에러 dict 가드는 테스트됨, 그러나 `fetch_company_screener`는 `_fmp_to_dataframe` 미사용 — 별도 가드 없음) | 중간 |
| `_parse_dart_accounts` OFS-only | `data_fetcher.py` | ✅ 있음 (`test_dart.py::test_parse_falls_back_to_ofs`) | 높음 |
| `_parse_dart_accounts` 계정명 변형 | `data_fetcher.py` | ❌ 없음 (모든 테스트가 `"당기순이익(손실)"` 정확 형태만 사용 — `"당기순이익"` 괄호 없는 변형 미테스트) | 높음 |
| `_safe(d, "key")` 문자열 값 | `screener.py` | ❌ 없음 (`"N/A"`, `""` 같은 비숫자 문자열을 전달하는 테스트 없음) | 중간 |
| `drawdown_alerts` 이중 호출 결과 동일성 | `signals.py` | ❌ 없음 (개별 동작 3개 테스트 존재, 동일 인자 두 번 호출 시 결과가 동일한지 명시 검증 없음) | 낮음 |
| `_discover_kr` KOSDAQ 실패 처리 | `universe.py` | ❌ 없음 (`test_discover_kr_filters_common_stocks_and_floor`는 happy path만 — `fetch_krx_daily("KOSDAQ", ...)` DataFetchError 전파 케이스 미테스트) | 높음 |

---

## 섹션 H — 성능·비용

### 담당자에게
불필요한 API 호출, 중복 계산, Turso 왕복이 비용과 속도에 미치는 영향을 분석합니다.
CLAUDE.md §4.10 (4): "성능은 추정 말고 측정. 비용은 한 차원만 보지 말 것."

### 읽어야 할 파일
```
src/signals.py           — generate_signal_report, factor_scores (L337-404)
src/digest.py            — send_daily_digest (L173-201)
src/screener.py          — screen_watchlist (L307-327)
src/data_fetcher.py      — TTL 상수 (L62-70)
```

### 체크리스트

#### H-1. `fetch_prices` 이중 호출 (`signals.py` L268-370)
```python
# factor_scores() 내부
closes = close_series(fetch_prices(ticker, period="1y"))   # 1차 호출 (캐시)

# generate_signal_report() 에서 factor_scores() 후
closes = close_series(fetch_prices(t, period="1y"))         # 2차 호출 (캐시 적중)
returns = closes.pct_change().dropna()
vols[t] = float(returns.std() * ...)
```
- [ ] `fetch_prices`의 캐시 TTL은 6시간(TTL_PRICES). 같은 실행 내에서는 캐시 적중이 보장됨.
      그러나 `factor_scores()` 내부에서 `closes`를 이미 계산했는데, `low_vol_score()` 결과가
      동일 계산을 하고 있음. 캐시는 맞지만 파이썬 연산이 중복됨. 성능 임팩트는?
- [ ] `generate_signal_report()`에서 `vols[t]`를 계산하는 코드가 `factor_scores()` 내부의
      `low_vol_score()`와 완전히 동일한 계산을 수행. `low_vol_score`의 `vol` 값을
      `FactorScores`에 담아 재사용하는 것이 낫지 않은가?

#### H-2. `send_daily_digest()` 중간 sync (`digest.py` L173-201)
```python
subscribers.ensure_owner()
text = build_daily_digest(...)   # 무거운 조립
recipients = subscribers.active_subscribers()

for chat_id, _name in recipients:
    send_safe(text, chat_id)

get_storage().sync()   # ← 전송 완료 후 클라우드 sync
```
- [ ] `build_daily_digest()` 내부에서 `generate_signal_report()` → `store.put_state(...)` →
      로컬 SQLite에 상태 저장. 이후 `sync()`로 클라우드에 push. 순서가 올바름.
- [ ] `build_daily_digest()` 호출 자체가 `sync()`를 포함하지 않으므로, 전송 도중 크래시 나면
      상태는 저장됐으나 클라우드에는 없는 상태. 이 경우 다음 실행에서 `first_run=False`로 돌지만
      상태 내용이 클라우드에 없어 변화 알림이 잘못 발화할 수 있는가?

#### H-3. `screen_watchlist()` API 호출 비용 분석 (`screener.py` L307-327)
- [ ] `US_WATCHLIST` 44개 티커 × (`fetch_quote` + `fetch_key_metrics` + `fetch_ratios`) = 최대 132 API 콜.
      캐시(30분/7일/7일 TTL)가 있으면 일 1회는 miss, 이후는 hit.
      `select_screened_tickers()` → `screen_watchlist()` → `generate_signal_report()` 에서
      이 경로가 FMP 쿼터에서 얼마나 차지하는가?

#### H-4. 발견 요약
| 항목 | 판정 | 메모 |
|---|---|---|
| `fetch_prices` 이중 호출 | [OK] | L368 주석(`# 캐시 적중 — 추가 호출 없음`)이 의도를 명시. TTL=6h로 2차 HTTP 호출 없음. 티커당 `pct_change+std` 연산 1회 중복되나 ~252행 기준 임팩트 미미 |
| `vol` 계산 중복 | [DESIGN] | `low_vol_score()`가 `vol` 값을 계산하지만 스코어만 반환 → `generate_signal_report()`에서 동일 계산(`pct_change+std*sqrt(252)*100`) 재수행. `FactorScores`에 `vol_pct: float` 필드 추가 후 `factor_scores()`에서 채우면 제거 가능. 성능 임팩트 미미 — DRY 위반 |
| sync 타이밍 리스크 | [DESIGN] | 전송 루프 중 크래시 시 `put_state()`(로컬 레플리카)는 완료됐으나 `sync()`(클라우드 push)는 미실행. 동일 `digest_replica.db` 재사용 재시작 시 로컬에 상태 있어 무해. 단, 레플리카 초기화(파일 삭제+cloud 재싱크) 시 최신 breach 상태 누락 → 오래된 상태 복원 → 중복 알림 가능. 발생 빈도 낮음(수동 복구 시에만) |
| 워치리스트 API 비용 | [OK] | 유니버스 DB 정상 운영 시 `top_symbols()`(오프라인 DB)가 선행 처리 → `screen_watchlist()` 미호출. 실질 일일 FMP 호출 ≈ top_n(6)×3=18회. DB 미구축 폴백 시 44×3+18=150회. TTL_STATEMENTS=7일 재무 캐싱으로 FMP 쿼터 보호 |

---

## 섹션 I — 미확인 파일

### 담당자에게
메인 분석에서 아직 읽지 않은 파일들입니다. 각 파일을 처음부터 읽고
동일한 체크리스트 형식으로 발견 사항을 기록해 주세요.

### 읽어야 할 파일 (전부 신규 분석)
```
src/predictors.py
src/logger.py
scripts/daily_update.py
scripts/scan.py
.github/workflows/daily-digest.yml
```

### 체크리스트

#### I-1. `src/predictors.py`
- [ ] `analyze_lead_lag()`: OLS R² 계산. R²가 **음수**일 수 있음(모델이 평균보다 나쁨).
      `reliable` 판정 기준(`p.r_squared >= 0.3`)이 음수를 걸러내는지 확인.
- [ ] lag 1~12월 중 best_lag 선택 방식: 단순 최대 R² 선택인가? 다중 비교로 인한
      과적합 리스크는 없는가? (12번 중 우연히 높은 R² 가 나올 확률)
- [ ] `PREDICTORS` 레지스트리 7개 관계: 이 관계들의 lead-lag가 학술적으로 검증된 것인가,
      데이터 기반으로 검증한 것인가? R² < 0.3 기준이 적절한가?
- [ ] `predict_*` 함수들이 fetch를 직접 호출 — 모듈 책임 원칙(CLAUDE.md §4.4)에서
      `predictors.py`의 `predict_*` 함수가 fetch를 가질 수 있도록 허용됨. 실제로 그런지 확인.

#### I-2. `src/logger.py`
- [ ] `TZFormatter`: KST 타임스탬프가 실제 서버 시간대(Oracle VM = UTC?)에서 올바르게
      출력되는지 확인. `QUANT_BOT_LOG_TZ` 미설정 시 기본값이 무엇인지.
- [ ] `get_logger(__name__)` 호출 패턴: 모든 src/*.py가 상단에서 이 패턴을 따르는지 grep 확인.
- [ ] 로그 파일 회전 (10MB × 5): 운영 서버에서 `logs/quant_bot.log` 경로가
      `QUANT_BOT_LOG_DIR` 환경변수로 제어되는지, 기본 경로가 올바른지.

#### I-3. `scripts/daily_update.py`
- [ ] 이 스크립트가 cron에서 실행되는지, 아니면 `send_digest.py`에 통합되었는지 확인.
- [ ] 모듈 별 실패가 전체를 막지 않도록 try/except로 격리되어 있는지.
- [ ] Turso sync를 마지막에 명시적으로 호출하는지.

#### I-4. `scripts/scan.py`
- [ ] `--check <ticker>` 옵션: `universe.lookup_detail()`로 ScoreCard 분해를 보여주는 흐름.
      `detail` JSON이 없는 종목(미보강)에서 graceful하게 처리되는지.
- [ ] 시장별 필터 없이 전체 스캔 시 CRYPTO와 KR/US 점수가 같은 기준으로 정렬되는 문제
      (CURRENT_STATE.md §5 한계 기록에 언급됨). 현재 코드가 시장별 정렬을 강제하는지.

#### I-5. `.github/workflows/daily-digest.yml`
- [ ] cron 스케줄: KST 08:30(한국 장 전), ET 09:00(미국 장 전) — UTC 변환이 올바른지.
- [ ] `--market` 파라미터가 KR/US 창에 따라 자동으로 전달되는지.
- [ ] **`--no-sync` 플래그**: 상시 봇이 실행 중일 때 cron이 이 플래그를 자동으로 쓰는지.
      아니면 수동으로 넣어야 하는지. (섹션 E-1과 연결)
- [ ] Secrets: `FRED_API_KEY`, `FMP_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
      `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN` 모두 등록되어 있는지 CURRENT_STATE.md §2와 대조.

#### I-6. 발견 요약
| 파일 | 항목 | 판정 | 메모 |
|---|---|---|---|
| `predictors.py` | R² 음수 처리 | [OK] | `scipy.linregress`의 `rvalue**2` 사용 — Pearson r²는 항상 0 이상(음수 불가). 일반 OLS R²(1−SS_res/SS_tot) 이 아니라 r²를 쓰므로 음수 발생 경로 없음. |
| `predictors.py` | 다중 비교 과적합 | [DESIGN] | lag 1~12 중 `max(|r|)`를 선택하는 방식은 데이터 스누핑 위험 내포. 모듈 docstring에 "표본이 작고 과최적화 위험이 있어 R²를 함께 제시"로 인지·명시됨. `reliable=r2>=0.30` 플래그와 ⚠️ 경고로 사용자가 신뢰도 직접 판단하도록 위임 — 기능 버그 없음. |
| `logger.py` | KST 타임스탬프 정확성 | [OK] | `TZFormatter.formatTime()`이 `record.created`(에포크 초)를 `ZoneInfo("Asia/Seoul")`로 변환 → UTC 서버에서도 KST+0900 오프셋 정확. `QUANT_BOT_LOG_TZ` 미설정 시 `"Asia/Seoul"` 기본값 사용. |
| `logger.py` | 로그 디렉토리 제어 | [OK] | `QUANT_BOT_LOG_DIR` 환경변수로 override 가능. 미설정 시 `PROJECT_ROOT/logs/`. `mkdir(parents=True, exist_ok=True)`로 자동 생성. `PROJECT_ROOT` = `Path(__file__).parent.parent` (= 프로젝트 루트) 계산 정확. |
| `daily_update.py` | 실패 격리 | [OK] | 각 섹션을 `QuantBotError` + broad `Exception`(noqa: BLE001) 이중 catch로 독립 격리. 실패 섹션은 `failures` 누적 후 exit code 1. Turso sync는 L166 전체 완료 후 명시 호출. |
| `scan.py` | 시장 혼합 정렬 | [OK] | `--market` 미지정 시 US/KR/CRYPTO를 별도 섹션으로 분리 출력 (시장별 `universe.scan()` 개별 호출). 서로 다른 점수 체계가 한 랭킹에 섞이지 않음. `--check` 시에도 `row.market` 기준으로 동일 시장 내 순위만 계산. `lookup_detail()` None 반환(미보강 종목) 시 `if detail:` 로 graceful 건너뜀. |
| `workflows/yml` | --no-sync 자동화 | [OK] | L78의 `subprocess.call([..., "--no-sync"])` 에 하드코딩 포함됨. 주석(L76-77)에 offset 경합 방지 목적 명시. cron 스케줄 자체는 Oracle systemd timer로 이전·비활성화 — `workflow_dispatch`(수동)만 활성. |
| `workflows/yml` | Secrets 완전성 | [OK] | 이 workflow가 실행하는 `send_digest.py`의 의존성(FRED, FMP, Telegram×2, Turso×2) 6종 전부 등록. KRX/DART는 `build_universe.py`에만 필요해 이 workflow에서는 불필요. |

---

## 전체 발견 집계 (검토 완료 후 작성)

### 버그 (즉시 수정) — ✅ 전부 수정 완료 (2026-06-29, 테스트 추가)
| 섹션 | 항목 | 파일:라인 | 수정 내용 | 상태 |
|---|---|---|---|---|
| B | `_safe()` 비숫자 문자열 ValueError | `screener.py` `_safe` | None/비숫자/NaN/Inf → default (try/except + `math.isnan/isinf`) | ✅ `test_safe_handles_non_numeric_and_nan` |
| B | `_safe()` NaN → `_clip` 통과로 만점 | `screener.py` `_safe` | 위와 동일 함수 — NaN 을 default 로 차단 | ✅ `test_nan_metric_scores_zero_not_full` |
| B | `fetch_company_screener` 200+에러dict → AttributeError | `data_fetcher.py` `fetch_company_screener` | `if not isinstance(data, list): return []` 가드 | ✅ `test_company_screener_rejects_error_dict` |
| B | DART 계정명 정확매칭 → 흑자기업 0점 | `data_fetcher.py` `_parse_dart_accounts` | `당기순이익(손실)` 우선 + `당기순이익` 폴백(0.0 보존 위해 명시 None 체크) | ✅ `test_parse_net_income_without_parens` |
| C | `_discover_kr` KOSDAQ 실패 전파 → CRYPTO 중단 | `universe.py` `_discover_kr` + `discover()` KR 블록 | KOSDAQ try/except(KOSPI 폴백) + KR 블록 try/except(US·CRYPTO 일관) | ✅ `test_discover_kr_survives_kosdaq_failure`, `test_discover_kr_block_isolates_failure` |

### 설계 의문 (옵션 제시 후 사용자 승인)
| 섹션 | 항목 | 현재 | 대안 A | 대안 B |
|---|---|---|---|---|
| | | | | |

### 테스트 추가 필요
| 섹션 | 함수/케이스 | 우선순위 |
|---|---|---|
| | | |

### 문서화 필요
| 항목 | 위치 |
|---|---|
| | |

---

*생성일: 2026-06-23 | 기준 커밋: `d2ccc29` (reply-keyboard + /subscribers)*
*이 파일 자체는 구현 코드가 아니므로 git에 커밋해도 무방합니다.*
