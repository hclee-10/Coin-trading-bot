import { Fragment, useState } from 'react'
import StorageWarning from './StorageWarning.jsx'

// 알고리즘 설명의 **강조** 를 굵게 그린다. 마크다운 라이브러리를 들이기에는
// 쓰는 문법이 이것 하나뿐이다.
function formatted(text) {
  return text.split('\n').map((line, lineIndex) => (
    <div key={lineIndex} style={{ minHeight: line ? undefined : '0.7em' }}>
      {line.split(/(\*\*[^*]+\*\*)/g).map((part, i) =>
        part.startsWith('**') && part.endsWith('**') ? (
          <strong key={i} style={{ color: 'var(--text)' }}>{part.slice(2, -2)}</strong>
        ) : (
          <span key={i}>{part}</span>
        ),
      )}
    </div>
  ))
}

const CATEGORY_LABELS = {
  trend: '추세추종',
  reversion: '평균회귀',
  breakout: '돌파',
  combo: '조합',
  range: '횡보',
}

function pct(value, digits = 2) {
  if (value === null || value === undefined) return '—'
  return `${value.toFixed(digits)}%`
}

function signed(value, digits = 2) {
  if (value === null || value === undefined) return '—'
  return `${value >= 0 ? '+' : ''}${value.toFixed(digits)}`
}

function since(ms) {
  if (!ms) return '—'
  const days = (Date.now() - ms) / 86_400_000
  if (days < 1) return `${Math.max(1, Math.round(days * 24))}시간`
  return `${Math.round(days)}일`
}

// 청산 위험도는 낮을수록 좋다. 색으로 바로 읽히게 한다.
function riskClass(value) {
  if (value >= 60) return 'neg'
  if (value >= 30) return 'warn-text'
  return ''
}

export default function Leaderboard({ data, catalog, onReset, busy, storage }) {
  const [expanded, setExpanded] = useState(null)
  const [confirming, setConfirming] = useState(false)
  const [typed, setTyped] = useState('')

  if (!data) return null
  const rows = data.strategies

  const algorithmOf = (name) =>
    catalog?.strategies?.find((s) => s.name === name)?.algorithm || ''

  return (
    <section className="panel">
      <h2>
        전략 경쟁 (모의매매)
        <span className="spacer" />
        <span className="hint">
          {rows.length}개 전략이 같은 시세로 동시에 매매 중
        </span>
      </h2>

      {/* 며칠씩 모아야 의미가 생기는 데이터다. 볼륨이 안 붙어 있으면 재배포
          한 번에 통째로 날아가므로, 표보다 먼저 눈에 띄어야 한다. */}
      {data.persistent === false && (
        <StorageWarning
          storage={storage}
          what="모의매매 성적"
          style={{ margin: '16px 16px 0' }}
        />
      )}

      {rows.length === 0 ? (
        <div className="empty">
          봇을 시작하면 모든 전략이 모의매매를 시작합니다.
        </div>
      ) : (
        <div style={{ overflowX: 'auto' }}>
          <table className="leaderboard">
            <thead>
              <tr>
                <th>#</th>
                <th>전략</th>
                <th>수익률</th>
                <th>수익금액</th>
                <th>수수료</th>
                <th>거래</th>
                <th>승률</th>
                <th>손절률</th>
                <th>청산위험</th>
                <th>최대낙폭</th>
                <th>기간</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((s, index) => (
                <Fragment key={s.name}>
                  <tr
                    onClick={() => setExpanded(expanded === s.name ? null : s.name)}
                    style={{ cursor: 'pointer' }}
                    className={s.name === data.active ? 'active-strategy' : ''}
                  >
                    <td>{index + 1}</td>
                    <td style={{ whiteSpace: 'nowrap' }}>
                      {s.name === data.active && <span className="badge on" style={{ marginRight: 6 }}>실거래</span>}
                      {s.name}
                      <div className="hint" style={{ fontSize: 11 }}>
                        {CATEGORY_LABELS[s.category] || s.category}
                        {s.open_positions > 0 && ` · 보유 ${s.open_positions}`}
                        {s.error && ' · ⚠️ 오류'}
                      </div>
                    </td>
                    <td className={s.return_pct >= 0 ? 'pos' : 'neg'}>
                      <strong>{signed(s.return_pct)}%</strong>
                    </td>
                    <td className={s.net_pnl >= 0 ? 'pos' : 'neg'}>{signed(s.net_pnl)}</td>
                    {/* 누적 수수료. 회전이 잦은 전략이 얼마를 갈아 넣고 있는지가
                        수익률만 봐서는 안 보인다. */}
                    <td className="hint">-{s.total_fee.toFixed(2)}</td>
                    <td>{s.trade_count}</td>
                    <td>{pct(s.win_rate, 0)}</td>
                    <td>{pct(s.stop_out_rate, 0)}</td>
                    <td className={riskClass(s.liquidation_risk_pct)}>
                      {pct(s.liquidation_risk_pct, 0)}
                    </td>
                    <td className="neg">{pct(s.max_drawdown_pct)}</td>
                    <td className="hint">{since(s.started_at)}</td>
                  </tr>
                  {expanded === s.name && (
                    <tr>
                      <td colSpan={11} style={{ textAlign: 'left', padding: 0 }}>
                        <div className="strategy-detail" style={{ margin: '0 16px 14px' }}>
                          <strong>{s.summary}</strong>
                          {s.error && (
                            <div className="banner error" style={{ margin: '10px 0' }}>
                              전략 오류: {s.error}
                            </div>
                          )}
                          <div className="hint" style={{ margin: '10px 0' }}>
                            {s.wins}승 {s.losses}패 · 최고 {signed(s.best_pnl)} ·
                            최악 {signed(s.worst_pnl)} · 수수료 {s.total_fee.toFixed(2)} ·
                            펀딩비 {s.total_funding.toFixed(2)} ·
                            가상 자기자본 {s.equity.toFixed(2)} / {s.start_equity.toFixed(0)}
                          </div>
                          <div style={{ lineHeight: 1.8 }}>
                            {formatted(algorithmOf(s.name))}
                          </div>
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="panel-body" style={{ borderTop: '1px solid var(--border)' }}>
        <p className="hint" style={{ marginTop: 0 }}>
          전략 이름을 누르면 알고리즘 상세가 열립니다. 모든 전략은 같은 시세와 같은
          사이징 규칙으로 매매합니다. 수수료는 항상 taker(0.05%)로, 펀딩비는 거래소의
          실제 비율로 8시간마다 부과합니다 — 모의 성적이 실제보다 좋아 보이면 판단이
          어긋나기 때문입니다.
        </p>
        {!confirming ? (
          <button className="ghost" style={{ fontSize: 12 }} onClick={() => setConfirming(true)}>
            기록 초기화
          </button>
        ) : (
          <div className="confirm-row">
            <span className="hint">
              모든 전략의 모의매매 기록을 지웁니다. 계속하려면 <strong>RESET</strong> 을 입력하세요.
            </span>
            <input
              value={typed}
              autoFocus
              placeholder="RESET"
              onChange={(e) => setTyped(e.target.value)}
            />
            <button
              className="danger"
              disabled={typed !== 'RESET' || busy}
              onClick={() => {
                onReset('RESET')
                setConfirming(false)
                setTyped('')
              }}
            >
              초기화
            </button>
            <button className="ghost" onClick={() => { setConfirming(false); setTyped('') }}>
              취소
            </button>
          </div>
        )}
      </div>
    </section>
  )
}
