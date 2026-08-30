function num(value, digits = 4) {
  if (value === null || value === undefined) return '—'
  return value.toLocaleString(undefined, { maximumFractionDigits: digits })
}

export default function Positions({ positions, source }) {
  return (
    <section className="panel">
      <h2>
        포지션
        <span className="spacer" />
        <span className="hint">
          {source === 'last_cycle' ? '마지막 주기 기준' : '거래소 조회'}
        </span>
      </h2>
      {positions.length === 0 ? (
        <div className="empty">보유 포지션이 없습니다</div>
      ) : (
        <table>
          <thead>
            <tr>
              <th>심볼</th>
              <th>방향</th>
              <th>수량</th>
              <th>진입가</th>
              <th>명목가</th>
              <th>미실현 손익</th>
              <th>청산가</th>
            </tr>
          </thead>
          <tbody>
            {positions.map((p) => (
              <tr key={p.symbol}>
                <td>{p.symbol}</td>
                <td className={p.side === 'long' ? 'pos' : 'neg'}>
                  {p.side === 'long' ? '롱' : '숏'}
                </td>
                <td>{num(p.contracts, 6)}</td>
                <td>{num(p.entry_price)}</td>
                <td>{num(p.notional, 2)}</td>
                <td className={p.unrealized_pnl >= 0 ? 'pos' : 'neg'}>
                  {p.unrealized_pnl >= 0 ? '+' : ''}{num(p.unrealized_pnl, 2)}
                </td>
                <td>{num(p.liquidation_price)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}
