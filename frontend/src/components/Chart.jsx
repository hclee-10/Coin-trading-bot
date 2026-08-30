import { useEffect, useRef } from 'react'
import { CandlestickSeries, createChart, createSeriesMarkers } from 'lightweight-charts'

// 대시보드 팔레트와 맞춘 색. styles.css 의 변수와 같은 값이다.
const COLORS = {
  up: '#26a69a',
  down: '#ef5350',
  text: '#8b98a5',
  grid: '#1c232c',
  background: '#0b0e13',
}

function toMarkers(markers) {
  return markers.map((m) => {
    const isEntry = m.kind === 'entry'
    const isLong = m.side === 'long'
    // 진입은 방향을, 청산은 손익을 색으로 나타낸다.
    const color = isEntry ? (isLong ? COLORS.up : COLORS.down) : m.pnl >= 0 ? COLORS.up : COLORS.down
    const label = isEntry
      ? isLong ? '롱 진입' : '숏 진입'
      : `청산 ${m.pnl >= 0 ? '+' : ''}${m.pnl.toFixed(2)}`
    return {
      time: m.time,
      position: isLong === isEntry ? 'belowBar' : 'aboveBar',
      color,
      shape: isEntry ? (isLong ? 'arrowUp' : 'arrowDown') : 'circle',
      text: label,
    }
  })
}

export default function Chart({ symbol, timeframe, candles, markers }) {
  const containerRef = useRef(null)
  const chartRef = useRef(null)
  const seriesRef = useRef(null)
  const markersRef = useRef(null)

  // 차트 인스턴스는 한 번만 만들고, 데이터만 갈아 끼운다.
  useEffect(() => {
    const chart = createChart(containerRef.current, {
      layout: {
        background: { color: COLORS.background },
        textColor: COLORS.text,
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: COLORS.grid },
        horzLines: { color: COLORS.grid },
      },
      rightPriceScale: { borderColor: COLORS.grid },
      timeScale: { borderColor: COLORS.grid, timeVisible: true },
      crosshair: { mode: 0 },
      height: 320,
      autoSize: true,
    })
    const series = chart.addSeries(CandlestickSeries, {
      upColor: COLORS.up,
      downColor: COLORS.down,
      borderUpColor: COLORS.up,
      borderDownColor: COLORS.down,
      wickUpColor: COLORS.up,
      wickDownColor: COLORS.down,
    })

    chartRef.current = chart
    seriesRef.current = series
    markersRef.current = createSeriesMarkers(series, [])

    return () => {
      chart.remove()
      chartRef.current = null
      seriesRef.current = null
      markersRef.current = null
    }
  }, [])

  useEffect(() => {
    if (!seriesRef.current || !candles?.length) return
    seriesRef.current.setData(candles)
  }, [candles])

  useEffect(() => {
    if (!markersRef.current) return
    markersRef.current.setMarkers(toMarkers(markers || []))
  }, [markers])

  const empty = !candles?.length

  return (
    <section className="panel">
      <h2>
        차트
        <span className="spacer" />
        <span className="hint">{symbol} · {timeframe}</span>
      </h2>
      {empty && (
        <div className="empty">
          봇을 시작하면 차트가 표시됩니다.
        </div>
      )}
      <div
        ref={containerRef}
        className="chart"
        style={{ height: empty ? 0 : 320, overflow: 'hidden' }}
      />
    </section>
  )
}
