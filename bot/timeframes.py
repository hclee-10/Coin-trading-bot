"""시간대(타임프레임) 변환 유틸리티.

다중 시간대 전략이 두 곳에서 상위 시간대 캔들을 얻는다:

* **실거래/모의매매** — 엔진이 거래소에서 상위 시간대 캔들을 직접 받아
  `StrategyContext.mtf_candles` 로 넣어 준다. 이쪽이 정확하다.
* **백테스트 등 공급이 없는 곳** — 기본 시간대 캔들을 여기 있는 `resample` 로
  묶어서 근사한다. 캔들이 모자라면 그 시간대는 계산되지 않을 뿐, 전략이
  죽지는 않아야 한다.

버킷 경계는 에포크 기준 정배수로 자른다 — 거래소의 1시간봉이 정시에 시작하는
것과 같은 규칙이라, 리샘플 결과가 거래소 캔들과 같은 경계를 갖는다.
"""

from __future__ import annotations

from bot.models import Candle

_UNIT_MS = {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}


def timeframe_to_ms(timeframe: str) -> int:
    """'5m', '1h', '4h', '1d' 같은 문자열을 밀리초로 바꾼다."""
    tf = timeframe.strip().lower()
    if len(tf) < 2 or tf[-1] not in _UNIT_MS:
        raise ValueError(f"알 수 없는 타임프레임 '{timeframe}' (예: 5m, 1h, 4h, 1d)")
    try:
        count = int(tf[:-1])
    except ValueError as exc:
        raise ValueError(f"알 수 없는 타임프레임 '{timeframe}'") from exc
    if count <= 0:
        raise ValueError("타임프레임 배수는 1 이상이어야 합니다")
    return count * _UNIT_MS[tf[-1]]


def resample(
    candles: list[Candle], timeframe: str, *, complete_only: bool = True
) -> list[Candle]:
    """작은 봉을 큰 봉으로 묶는다 (예: 5분봉 → 1시간봉).

    `complete_only=True` 면 아직 다 차지 않은 마지막 버킷을 버린다 — 진행 중인
    상위 봉은 값이 계속 바뀌므로, 지표 계산에는 확정된 봉만 쓰는 것이 안전하다.
    """
    if not candles:
        return []
    bucket_ms = timeframe_to_ms(timeframe)

    out: list[Candle] = []
    current: Candle | None = None
    for c in candles:
        start = c.timestamp - (c.timestamp % bucket_ms)
        if current is None or current.timestamp != start:
            if current is not None:
                out.append(current)
            current = Candle(
                timestamp=start, open=c.open, high=c.high, low=c.low,
                close=c.close, volume=c.volume,
            )
        else:
            current = Candle(
                timestamp=current.timestamp,
                open=current.open,
                high=max(current.high, c.high),
                low=min(current.low, c.low),
                close=c.close,
                volume=current.volume + c.volume,
            )
    if current is not None:
        out.append(current)

    if complete_only and out:
        # 소스 봉의 간격으로 마지막 버킷이 끝까지 찼는지 판단한다.
        source_ms = (
            candles[1].timestamp - candles[0].timestamp if len(candles) > 1 else 0
        )
        last_end = candles[-1].timestamp + source_ms
        if source_ms <= 0 or last_end < out[-1].timestamp + bucket_ms:
            out.pop()
    return out
