function money(value, currency) {
  if (value === null || value === undefined) return '—'
  return `${value.toLocaleString(undefined, { maximumFractionDigits: 2 })} ${currency}`
}

function timeAgo(iso) {
  if (!iso) return '아직 없음'
  const seconds = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000))
  if (seconds < 60) return `${seconds}초 전`
  if (seconds < 3600) return `${Math.round(seconds / 60)}분 전`
  return `${Math.round(seconds / 3600)}시간 전`
}

export default function StatusCards({ status }) {
  const { equity, day_start_equity: dayStart, quote_currency: currency } = status
  const pnl = equity !== null && dayStart ? equity - dayStart : null
  const pnlPct = pnl !== null && dayStart ? (pnl / dayStart) * 100 : null
  const pnlClass = pnl === null ? '' : pnl >= 0 ? 'pos' : 'neg'

  return (
    <div className="cards">
      <div className="card">
        <div className="label">자기자본</div>
        <div className="value">{money(equity, currency)}</div>
        <div className="sub">{status.exchange.toUpperCase()} 선물 계좌</div>
      </div>
      <div className="card">
        <div className="label">오늘 손익 (UTC 기준)</div>
        <div className={`value ${pnlClass}`}>
          {pnl === null ? '—' : `${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}`}
        </div>
        <div className="sub">
          {pnlPct === null ? '기준값 대기 중' : `${pnlPct >= 0 ? '+' : ''}${pnlPct.toFixed(2)}%`}
        </div>
      </div>
      <div className="card">
        <div className="label">보유 포지션</div>
        <div className="value">{status.open_positions}</div>
        <div className="sub">{status.symbols.length}개 심볼 감시 중</div>
      </div>
      <div className="card">
        <div className="label">마지막 주기</div>
        <div className="value" style={{ fontSize: 16 }}>{timeAgo(status.last_cycle_at)}</div>
        <div className="sub">{status.strategy} · {status.timeframe}</div>
      </div>
    </div>
  )
}
