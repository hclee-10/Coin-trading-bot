import { useState } from 'react'

const CATEGORY_LABELS = {
  trend: '추세추종',
  reversion: '평균회귀',
  breakout: '돌파',
  combo: '조합',
  range: '횡보 전용',
}

export default function StrategyInfo({ catalog }) {
  const [expanded, setExpanded] = useState(false)
  if (!catalog) return null

  const active = catalog.strategies.find((s) => s.name === catalog.active)

  return (
    <section className="panel">
      <h2>
        전략
        <span className="spacer" />
        <span className="hint">{catalog.active}</span>
      </h2>
      <div className="panel-body">
        {!active ? (
          <p className="hint" style={{ margin: 0 }}>
            <strong>{catalog.active}</strong> 는 매매하지 않는 전략입니다.
            배선을 확인하는 용도이며, 실제로 사고팔려면 다른 전략을 설정하세요.
          </p>
        ) : (
          <>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
              <span className="badge">{CATEGORY_LABELS[active.category] || active.category}</span>
              <strong>{active.summary}</strong>
            </div>
            {expanded && (
              <pre className="strategy-detail">{active.description}</pre>
            )}
            <button
              className="ghost"
              style={{ padding: '4px 10px', fontSize: 12, marginTop: 8 }}
              onClick={() => setExpanded((v) => !v)}
            >
              {expanded ? '설명 접기' : '자세한 설명'}
            </button>
          </>
        )}

        <details style={{ marginTop: 16 }}>
          <summary className="hint" style={{ cursor: 'pointer' }}>
            사용 가능한 전략 {catalog.strategies.length}개
          </summary>
          <table style={{ marginTop: 10 }}>
            <tbody>
              {catalog.strategies.map((s) => (
                <tr key={s.name} className={s.name === catalog.active ? 'pos' : ''}>
                  <td style={{ whiteSpace: 'nowrap' }}>{s.name}</td>
                  <td style={{ textAlign: 'left', color: 'var(--muted)' }}>
                    {CATEGORY_LABELS[s.category] || s.category}
                  </td>
                  <td style={{ textAlign: 'left' }}>{s.summary}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      </div>
    </section>
  )
}
