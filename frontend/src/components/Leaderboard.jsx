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

function loadHideLosers() {
  try {
    return localStorage.getItem('leaderboard.hideLosers') === '1'
  } catch {
    return false
  }
}

export default function Leaderboard({ data, catalog, onReset, busy, storage }) {
  const [expanded, setExpanded] = useState(null)
  const [confirming, setConfirming] = useState(false)
  const [typed, setTyped] = useState('')
  // 거래는 했는데 이긴 적이 한 번도 없는 전략을 접는다. 거래 0건(아직 판단
  // 불가)은 숨기지 않는다 — 실패한 것과 안 해 본 것은 다르다.
  const [hideLosers, setHideLosers] = useState(loadHideLosers)

  const toggleHideLosers = () => {
    setHideLosers((current) => {
      try {
        localStorage.setItem('leaderboard.hideLosers', current ? '0' : '1')
      } catch {
        // 저장 실패는 무시 — 토글 자체는 동작해야 한다
      }
      return !current
    })
  }

  if (!data) return null
  const allRows = data.strategies
  const rows = hideLosers
    ? allRows.filter((s) => !(s.trade_count > 0 && s.wins === 0))
    : allRows
  const hiddenCount = allRows.length - rows.length

  const algorithmOf = (name) =>
    catalog?.strategies?.find((s) => s.name === name)?.algorithm || ''

  return (
    <section className="panel">
      <h2>
        전략 경쟁 (모의매매)
        <span className="spacer" />
        <label className="hint" style={{ cursor: 'pointer', marginRight: 12 }}>
          <input
            type="checkbox"
            checked={hideLosers}
            onChange={toggleHideLosers}
            style={{ verticalAlign: 'middle', marginRight: 4 }}
          />
          승률 0% 숨기기{hiddenCount > 0 && ` (${hiddenCount})`}
        </label>
        <span className="hint">
          {allRows.length}개 전략이 같은 시세로 동시에 매매 중
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

      {allRows.length === 0 ? (
        <div className="empty">
          봇을 시작하면 모든 전략이 모의매매를 시작합니다.
        </div>
      ) : rows.length === 0 ? (
        <div className="empty">승률 0% 필터로 모든 전략이 숨겨졌습니다.</div>
      ) : (
        <div style={{ overflowX: 'auto' }}>
          <table className="leaderboard">
            <thead>
              <tr>
                <th>#</th>
                <th>전략</th>
                <th>수익률</th>
                <th>실현손익</th>
                <th>평가손익</th>
                <th>수수료</th>
                <th>거래</th>
                <th>롱/숏</th>
                <th>승률</th>
                <th>손절률</th>
                <th>필요자본</th>
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
                      {s.name === data.active && (
                        <span className={data.live ? 'badge on' : 'badge'} style={{ marginRight: 6 }}>
                          {data.live ? '실거래' : '지정 전략'}
                        </span>
                      )}
                      {s.name}
                      <div className="hint" style={{ fontSize: 11 }}>
                        {CATEGORY_LABELS[s.category] || s.category}
                        {s.position_amount > 0 && (
                          <>
                            {' · '}
                            <span className={s.position_side === 'long' ? 'pos' : 'neg'}>
                              {s.position_side === 'long' ? '롱' : '숏'}
                            </span>
                            {` ${s.position_notional.toFixed(1)}$ @ ${s.position_entry.toLocaleString(undefined, { maximumFractionDigits: 1 })}`}
                          </>
                        )}
                        {s.error && ' · ⚠️ 오류'}
                      </div>
                    </td>
                    <td className={s.return_pct >= 0 ? 'pos' : 'neg'}>
                      <strong>{signed(s.return_pct)}%</strong>
                    </td>
                    {/* 실현 = 닫힌 거래에서 확정된 돈. 평가 = 지금 들고 있는
                        포지션이 물려 있는(또는 벌고 있는) 돈. 합쳐 놓으면
                        어디서 손실이 나는지 안 보여서 분리했다. */}
                    <td className={(s.realized_pnl ?? 0) >= 0 ? 'pos' : 'neg'}>
                      {signed(s.realized_pnl ?? s.net_pnl - s.unrealized)}
                    </td>
                    <td className={s.unrealized >= 0 ? 'pos' : 'neg'}>
                      {signed(s.unrealized)}
                    </td>
                    {/* 누적 수수료. 회전이 잦은 전략이 얼마를 갈아 넣고 있는지가
                        수익률만 봐서는 안 보인다. */}
                    <td className="hint">-{s.total_fee.toFixed(2)}</td>
                    <td>{s.trade_count}</td>
                    {/* 주문 방향별 횟수. 적립식은 주문 여러 개가 포지션 하나로
                        합쳐지므로 거래(왕복) 수만으로는 보이지 않는다. */}
                    <td style={{ whiteSpace: 'nowrap' }}>
                      <span className="pos">{s.long_orders ?? 0}</span>
                      <span className="hint"> / </span>
                      <span className="neg">{s.short_orders ?? 0}</span>
                    </td>
                    <td>{pct(s.win_rate, 0)}</td>
                    <td>{pct(s.stop_out_rate, 0)}</td>
                    {/* 청산 안 당하고 버티는 데 지금까지 필요했던 최소 자기자본
                        (증거금 + 최악 순간의 평가손실). 이 실험의 핵심 답이다. */}
                    <td className="hint" style={{ whiteSpace: 'nowrap' }}>
                      {s.required_equity > 0 ? `${Math.ceil(s.required_equity).toLocaleString()}$` : '—'}
                    </td>
                    <td className={riskClass(s.liquidation_risk_pct)}>
                      {pct(s.liquidation_risk_pct, 0)}
                    </td>
                    <td className="neg">{pct(s.max_drawdown_pct)}</td>
                    <td className="hint">{since(s.started_at)}</td>
                  </tr>
                  {expanded === s.name && (
                    <tr>
                      <td colSpan={14} style={{ textAlign: 'left', padding: 0 }}>
                        <div className="strategy-detail" style={{ margin: '0 16px 14px' }}>
                          <strong>{s.summary}</strong>
                          {s.error && (
                            <div className="banner error" style={{ margin: '10px 0' }}>
                              전략 오류: {s.error}
                            </div>
                          )}
                          <div className="hint" style={{ margin: '10px 0' }}>
                            {s.wins}승 {s.losses}패 ·{' '}
                            <span className="pos">롱</span> 주문 {s.long_orders ?? 0}회
                            {s.long_avg_price > 0 &&
                              ` (평균 ${s.long_avg_price.toLocaleString(undefined, { maximumFractionDigits: 1 })} · 합계 ${(s.long_notional ?? 0).toFixed(0)}$)`}
                            {' · '}
                            <span className="neg">숏</span> 주문 {s.short_orders ?? 0}회
                            {s.short_avg_price > 0 &&
                              ` (평균 ${s.short_avg_price.toLocaleString(undefined, { maximumFractionDigits: 1 })} · 합계 ${(s.short_notional ?? 0).toFixed(0)}$)`}
                            {' '}· 최고 {signed(s.best_pnl)} ·
                            최악 {signed(s.worst_pnl)} · 수수료 {s.total_fee.toFixed(2)} ·
                            펀딩비 {s.total_funding.toFixed(2)} ·
                            가상 자기자본 {s.equity.toFixed(2)} / {s.start_equity.toFixed(0)}
                          </div>
                          {/* 주문 기록은 이 기능이 배포된 시점부터 시작됐다. 그 전에
                              쌓인 포지션은 롱/숏 횟수·평균가에 안 잡히므로, 기록된
                              주문 합계로 설명이 안 되는 큰 포지션이 있으면 그 사실을
                              말해 줘야 한다 — 아니면 "평균가는 수익권인데 왜 평가손실이
                              크지?" 라는 착시가 생긴다. */}
                          {s.position_amount > 0 &&
                            s.position_notional >
                              ((s.long_notional ?? 0) + (s.short_notional ?? 0)) * 1.2 && (
                            <div className="hint" style={{ margin: '10px 0' }}>
                              ⚠️ 보유 포지션({s.position_notional.toFixed(0)}$)의 대부분은{' '}
                              <strong style={{ color: 'var(--text)' }}>주문 기록 시작 전</strong>에
                              쌓였습니다. 위의 롱/숏 횟수·평균가는 기록된 주문
                              (합계 {(((s.long_notional ?? 0) + (s.short_notional ?? 0))).toFixed(0)}$)만
                              반영하므로, 평가손익은 포지션 평단
                              ({s.position_entry.toLocaleString(undefined, { maximumFractionDigits: 1 })})
                              기준으로 봐야 합니다. 기록 초기화를 하면 모든 숫자가 같은
                              시점부터 다시 계산됩니다.
                            </div>
                          )}
                          <div className="hint" style={{ margin: '10px 0' }}>
                            손익 구성: 실현{' '}
                            <span className={(s.realized_pnl ?? 0) >= 0 ? 'pos' : 'neg'}>
                              {signed(s.realized_pnl ?? s.net_pnl - s.unrealized)}
                            </span>
                            {` (닫힌 거래 ${s.trade_count}건에서 확정)`}
                            {' + '}평가{' '}
                            <span className={s.unrealized >= 0 ? 'pos' : 'neg'}>
                              {signed(s.unrealized)}
                            </span>
                            {' (보유 포지션을 지금 닫으면 — 수수료·펀딩비 포함)'}
                            {' = '}
                            <strong style={{ color: 'var(--text)' }}>{signed(s.net_pnl)}</strong>
                          </div>
                          {s.required_equity > 0 && (
                            <div className="hint" style={{ margin: '10px 0' }}>
                              청산을 버티는 데 필요했던 최소 자본:{' '}
                              <strong style={{ color: 'var(--text)' }}>
                                {Math.ceil(s.required_equity).toLocaleString()} USDT
                              </strong>
                              {' '}(포지션 증거금 + 최악 순간의 평가손실, {data.leverage}배 기준)
                            </div>
                          )}
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
