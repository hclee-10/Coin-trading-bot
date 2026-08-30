function money(value, digits = 2) {
  if (value === null || value === undefined) return '—'
  return value.toLocaleString(undefined, { maximumFractionDigits: digits })
}

function signed(value, digits = 2) {
  if (value === null || value === undefined) return '—'
  return `${value >= 0 ? '+' : ''}${money(value, digits)}`
}

function when(ms) {
  if (!ms) return '—'
  return new Date(ms).toLocaleString()
}

export default function Performance({ performance, currency }) {
  if (!performance) return null

  const {
    trade_count: trades,
    win_count: wins,
    loss_count: losses,
    win_rate: winRate,
    realized_pnl: realized,
    total_fee: fees,
    total_return_pct: returnPct,
    equity_change: change,
    started_at: startedAt,
    persistent,
    trades: history,
  } = performance

  const changeClass = change === null || change === undefined ? '' : change >= 0 ? 'pos' : 'neg'

  return (
    <>
      <section className="panel">
        <h2>
          자동매매 성과
          <span className="spacer" />
          <span className="hint">
            {startedAt ? `${when(startedAt)} 기록 시작` : '아직 기록 없음'}
          </span>
        </h2>
        <div className="panel-body">
          {!persistent && (
            <div className="banner warn" style={{ marginBottom: 14 }}>
              <strong>기록이 메모리에만 남습니다.</strong>
              재배포하면 성과 기록이 사라집니다. Railway 에서 볼륨을 <code>/data</code> 에
              마운트하면 유지됩니다.
            </div>
          )}
          <div className="cards" style={{ marginBottom: 0 }}>
            <div className="card">
              <div className="label">누적 수익률</div>
              <div className={`value ${changeClass}`}>
                {returnPct === null || returnPct === undefined
                  ? '—'
                  : `${returnPct >= 0 ? '+' : ''}${returnPct.toFixed(2)}%`}
              </div>
              <div className="sub">자기자본 {signed(change)} {currency}</div>
            </div>
            <div className="card">
              <div className="label">실현 손익</div>
              <div className={`value ${realized >= 0 ? 'pos' : 'neg'}`}>{signed(realized)}</div>
              <div className="sub">수수료 {money(fees)} 포함</div>
            </div>
            <div className="card">
              <div className="label">승률</div>
              <div className="value">
                {winRate === null || winRate === undefined ? '—' : `${winRate.toFixed(0)}%`}
              </div>
              <div className="sub">{wins}승 {losses}패 · {trades}거래</div>
            </div>
          </div>
        </div>
      </section>

      <section className="panel">
        <h2>
          거래 내역
          <span className="spacer" />
          <span className="hint">최근 {history.length}건</span>
        </h2>
        {history.length === 0 ? (
          <div className="empty">아직 완료된 거래가 없습니다</div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table>
              <thead>
                <tr>
                  <th>청산 시각</th>
                  <th>방향</th>
                  <th>수량</th>
                  <th>진입가</th>
                  <th>청산가</th>
                  <th>수익률</th>
                  <th>손익</th>
                </tr>
              </thead>
              <tbody>
                {history.map((t) => (
                  <tr key={`${t.opened_at}-${t.closed_at}`}>
                    <td>{when(t.closed_at)}</td>
                    <td className={t.side === 'long' ? 'pos' : 'neg'}>
                      {t.side === 'long' ? '롱' : '숏'}
                    </td>
                    <td>{money(t.amount, 6)}</td>
                    <td>{money(t.entry_price, 4)}</td>
                    <td>{money(t.exit_price, 4)}</td>
                    <td className={t.return_pct >= 0 ? 'pos' : 'neg'}>
                      {signed(t.return_pct)}%
                    </td>
                    <td className={t.pnl >= 0 ? 'pos' : 'neg'}>{signed(t.pnl)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  )
}
