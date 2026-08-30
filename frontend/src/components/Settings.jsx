// "지금 얼마로 매매하고 있나" 를 화면에서 바로 답할 수 있게 하는 패널.
//
// 이 값들은 서버의 실제 설정(config)에서 그대로 온다. 코드 기본값이 아니라
// Railway 의 CONFIG_YAML 이 적용된 결과이므로, 배포한 설정이 의도와 다르면
// 여기서 바로 보인다 — 실제로 익절이 다시 켜져 있는 사고를 잡기 위한 패널이다.

const TIER_NAMES = ['낮음 (LOW)', '보통 (MEDIUM)', '높음 (HIGH)', '매우 높음 (VERY_HIGH)']

function num(value, digits = 2) {
  if (value === null || value === undefined) return '—'
  return value.toLocaleString(undefined, { maximumFractionDigits: digits })
}

export default function Settings({ config, equity }) {
  if (!config) return null

  const { risk, trading, exchange } = config
  const tiers = risk.sizing_mode === 'tiers' ? risk.notional_tiers || [] : []
  const takeProfit = risk.default_take_profit_pct || 0

  // 등급 금액이 그대로 나가는지, 상한에 잘려서 더 작게 나가는지가 실제로 중요하다.
  // 자기자본이 작으면 200 달러 등급이 조용히 깎인다.
  const caps = []
  if (equity) {
    caps.push(equity * (risk.max_position_notional_pct / 100))
    caps.push(equity * Math.min(exchange.leverage, risk.max_leverage))
  }
  const cap = caps.length > 0 ? Math.min(...caps) : null
  const floor = risk.min_order_notional

  return (
    <section className="panel">
      <h2>
        거래 설정
        <span className="spacer" />
        <span className="hint">서버에 적용된 실제 값</span>
      </h2>
      <div className="panel-body">
        {takeProfit > 0 && (
          <div className="banner warn" style={{ marginBottom: 14 }}>
            <strong>익절(수익률 제한)이 켜져 있습니다 — {takeProfit}%</strong>
            수익률 제한 없이 굴리려면 CONFIG_YAML 의 <code>default_take_profit_pct</code> 를
            <code>0</code> 으로 바꾸세요.
          </div>
        )}
        {risk.sizing_mode !== 'tiers' && (
          <div className="banner warn" style={{ marginBottom: 14 }}>
            <strong>주문 금액이 등급 방식이 아닙니다 — {risk.sizing_mode}</strong>
            50/100/150/200 등급으로 넣으려면 CONFIG_YAML 에{' '}
            <code>sizing_mode: tiers</code> 를 넣으세요.
          </div>
        )}

        {tiers.length > 0 && (
          <>
            <h3 className="hint" style={{ margin: '0 0 8px' }}>확신도별 1회 주문 금액</h3>
            <table>
              <thead>
                <tr>
                  <th>확신도</th>
                  <th>신호 강도</th>
                  <th>주문 명목가</th>
                  <th>실제 적용</th>
                </tr>
              </thead>
              <tbody>
                {tiers.map((value, i) => {
                  const name = tiers.length === 4 ? TIER_NAMES[i] : `${i + 1}등급`
                  const lower = (i / tiers.length).toFixed(2)
                  const upper = ((i + 1) / tiers.length).toFixed(2)
                  const effective = cap === null ? value : Math.min(value, cap)
                  const clipped = effective < value - 1e-9
                  const blocked = effective < floor
                  return (
                    <tr key={i}>
                      <td>{name}</td>
                      <td className="hint">{lower} ~ {upper}</td>
                      <td>{num(value, 0)} USDT</td>
                      <td className={`${blocked ? 'neg' : clipped ? 'warn-text' : ''}`}>
                        {blocked
                          ? '주문 안 됨 (최소금액 미만)'
                          : clipped
                            ? `${num(effective, 0)} USDT (상한에 잘림)`
                            : '그대로'}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
            <p className="hint" style={{ marginTop: 8 }}>
              {cap === null
                ? '자기자본을 아직 못 읽어서 상한 계산은 생략했습니다.'
                : `현재 자기자본 ${num(equity)} USDT 기준 1포지션 명목가 상한은 ${num(cap, 0)} USDT 입니다.`}
            </p>
          </>
        )}

        <div className="cards" style={{ marginTop: 16, marginBottom: 0 }}>
          <div className="card">
            <div className="label">손절</div>
            <div className="value">{risk.default_stop_loss_pct}%</div>
            <div className="sub">전략이 손절가를 안 주면 이 폭으로</div>
          </div>
          <div className="card">
            <div className="label">익절</div>
            <div className={`value ${takeProfit > 0 ? 'warn-text' : ''}`}>
              {takeProfit > 0 ? `${takeProfit}%` : '없음'}
            </div>
            <div className="sub">{takeProfit > 0 ? '수익률이 제한됩니다' : '수익률 제한 없음'}</div>
          </div>
          <div className="card">
            <div className="label">레버리지</div>
            <div className="value">{exchange.leverage}x</div>
            <div className="sub">{exchange.margin_mode} · 한도 {risk.max_leverage}x</div>
          </div>
          <div className="card">
            <div className="label">동시 보유</div>
            <div className="value">{risk.max_open_positions}개</div>
            <div className="sub">최소 주문 {num(floor, 0)} USDT</div>
          </div>
          <div className="card">
            <div className="label">일일 손실 한도</div>
            <div className="value">{risk.max_daily_loss_pct}%</div>
            <div className="sub">넘으면 신규 진입 차단(킬스위치)</div>
          </div>
          <div className="card">
            <div className="label">주문 방식</div>
            <div className="value" style={{ fontSize: 18 }}>
              {trading.order_type === 'limit' ? '지정가' : '시장가'}
            </div>
            <div className="sub">
              {trading.order_type === 'limit'
                ? `현재가 ±${trading.limit_offset_pct}% · ${num(trading.limit_timeout_sec, 0)}초 내 미체결 시 ${
                    trading.limit_fallback_market ? '시장가로 전환' : '취소'
                  }`
                : '즉시 체결, taker 수수료'}
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}
