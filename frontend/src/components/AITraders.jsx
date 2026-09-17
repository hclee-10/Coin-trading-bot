import { useState } from 'react'
import { api } from '../api.js'

// AI 경쟁 매매 — 4개의 AI(클로드/GPT/그록/제미나이)가 각자 전용 토큰으로
// 자기 봇을 프로그래밍해 경쟁한다. 사람이 하는 일은 두 가지뿐이다:
// ① 지시서(규칙+토큰+API 사용법) 복사해서 AI 에게 전달
// ② API 를 못 부르는 AI 가 답으로 준 스펙 JSON 을 붙여넣기

function signed(value, digits = 2) {
  if (value === null || value === undefined) return '—'
  return `${value >= 0 ? '+' : ''}${value.toFixed(digits)}`
}

function TraderCard({ trader, row, busy }) {
  const [note, setNote] = useState('')
  const [handoffState, setHandoffState] = useState('')  // '' | loading | copied | manual
  const [handoffText, setHandoffText] = useState('')
  const [specText, setSpecText] = useState('')
  const [specOpen, setSpecOpen] = useState(false)

  const copyHandoff = async () => {
    setHandoffState('loading')
    try {
      const { markdown } = await api.aiHandoff(trader.name)
      setHandoffText(markdown)
      try {
        await navigator.clipboard.writeText(markdown)
        setHandoffState('copied')
      } catch {
        setHandoffState('manual')
      }
    } catch (err) {
      setHandoffState('')
      setNote(err.message || '지시서 생성 실패')
    }
  }

  const applySpec = async () => {
    let parsed
    try {
      // AI 가 코드블록째로 준 것도 받아 준다.
      const cleaned = specText.replace(/^```[a-z]*\n?/m, '').replace(/```\s*$/m, '')
      parsed = JSON.parse(cleaned)
    } catch {
      setNote('JSON 을 읽을 수 없습니다 — 코드블록 내용만 붙여넣으세요')
      return
    }
    try {
      await api.aiSpec(trader.name, parsed)
      setNote('✓ 봇 스펙 적용됨 — 15초 주기로 실행됩니다')
      setSpecText('')
      setSpecOpen(false)
    } catch (err) {
      setNote(err.message || '스펙 적용 실패')
    }
  }

  const spec = trader.spec || {}
  const ruleCount = spec.rules?.length || 0

  return (
    <div style={{ border: '1px solid var(--border)', borderRadius: 8, padding: '12px 14px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
        <strong>{trader.label}</strong>
        {row?.bankrupt && (
          <span className="badge" style={{ background: 'var(--red, #b33)' }}>파산 · 실격</span>
        )}
        {row ? (
          <span className="hint">
            수익률 <span className={row.return_pct >= 0 ? 'pos' : 'neg'}>
              {signed(row.return_pct)}%
            </span>
            {row.vs_market_pct !== null && row.vs_market_pct !== undefined && (
              <>
                {' · 시장대비 '}
                <span className={row.vs_market_pct >= -5 ? (row.vs_market_pct >= 0 ? 'pos' : '') : 'neg'}>
                  {signed(row.vs_market_pct)}%p
                </span>
                {row.vs_market_pct < -5 && ' ⚠️ 노출증가 차단'}
              </>
            )}
            {row.position_amount > 0 ? (
              <>
                {' · '}
                <span className={row.position_side === 'long' ? 'pos' : 'neg'}>
                  {row.position_side === 'long' ? '롱' : '숏'}
                </span>
                {` ${row.position_notional.toFixed(0)}$ @ ${row.position_entry.toLocaleString(undefined, { maximumFractionDigits: 1 })}`}
              </>
            ) : ' · 포지션 없음'}
          </span>
        ) : (
          <span className="hint">아직 기록 없음</span>
        )}
        <span className="spacer" />
        <button className="ghost" style={{ fontSize: 12 }} onClick={copyHandoff}
                disabled={handoffState === 'loading'}>
          {handoffState === 'loading' ? '생성 중…'
            : handoffState === 'copied' ? '✓ 복사됨'
            : '지시서+토큰 복사'}
        </button>
        <button className="ghost" style={{ fontSize: 12 }} onClick={() => setSpecOpen(!specOpen)}>
          스펙 붙여넣기
        </button>
      </div>

      <div className="hint" style={{ marginTop: 8 }}>
        봇 상태: 레버리지 {spec.leverage ?? 3}배 · 규칙 {ruleCount}개
        {spec.stop_loss_pct > 0 && ` · 손절 ${spec.stop_loss_pct}%`}
        {spec.take_profit_pct > 0 && ` · 익절 ${spec.take_profit_pct}%`}
        {trader.pending.length > 0 && ` · 대기 주문 ${trader.pending.length}건`}
        {trader.spec_next_allowed_ms > 0 && (
          ` · 다음 스펙 수정 가능: ${new Date(trader.spec_next_allowed_ms).toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' })}`
        )}
        {ruleCount === 0 && trader.pending.length === 0 && ' — 아직 스펙 없음 (지시서를 AI에게 전달하세요)'}
        {spec.memo && (
          <div style={{ marginTop: 4 }}>메모: {spec.memo}</div>
        )}
      </div>

      {specOpen && (
        <div style={{ marginTop: 8 }}>
          <textarea
            value={specText}
            onChange={(e) => setSpecText(e.target.value)}
            placeholder='AI 가 답으로 준 봇 스펙 JSON 을 여기 붙여넣으세요'
            style={{ width: '100%', minHeight: 120, fontSize: 12, fontFamily: 'monospace' }}
          />
          <div className="confirm-row" style={{ marginTop: 6 }}>
            <button disabled={busy || !specText.trim()} onClick={applySpec}>스펙 적용</button>
            <button className="ghost" onClick={() => { setSpecOpen(false); setSpecText('') }}>
              닫기
            </button>
          </div>
        </div>
      )}

      {note && <div className="hint" style={{ marginTop: 6 }}>{note}</div>}

      {handoffText && handoffState !== '' && (
        <details open={handoffState === 'manual'} style={{ marginTop: 8 }}>
          <summary className="hint" style={{ cursor: 'pointer' }}>
            {handoffState === 'manual'
              ? '복사 권한이 없어요 — 아래 내용을 직접 복사하세요'
              : '지시서 내용 보기 (토큰 포함 — 해당 AI에게만 전달)'}
          </summary>
          <textarea
            readOnly value={handoffText}
            style={{ width: '100%', minHeight: 180, marginTop: 6, fontSize: 12 }}
            onFocus={(e) => e.target.select()}
          />
        </details>
      )}
    </div>
  )
}

export default function AITraders({ state, leaderboard, busy }) {
  if (!state) return null
  const rowOf = (name) => leaderboard?.strategies?.find((s) => s.name === name)

  return (
    <section className="panel">
      <h2>
        AI 경쟁 매매
        <span className="spacer" />
        <span className="hint">클로드 · GPT · 그록 · 제미나이 — 각자 봇을 직접 프로그래밍</span>
      </h2>
      <div className="panel-body" style={{ display: 'grid', gap: 12 }}>
        <p className="hint" style={{ margin: 0 }}>
          <strong>지시서+토큰 복사</strong>를 눌러 각 AI 에게 한 번 전달하세요. 지시서에는
          그 AI 가 자기 예약(루틴/Tasks) 기능에 <strong>6시간 주기 자동 갱신</strong>을
          등록하는 방법까지 들어 있어 — 등록만 되면 사람 개입 없이 돌아갑니다. URL 열기만
          되는 AI 도 참가 가능하고(GET 제출 지원), 그것도 안 되면 스펙 JSON 을 답으로 받아
          <strong> 스펙 붙여넣기</strong>에 넣으면 됩니다. 규칙: 자금 1만 USDT · 레버리지 최대 10배 · 회당 5,000 USDT ·
          스펙 수정 6시간당 1회 · 시장 대비 −5%p 아래면 노출 증가 차단 · 파산(자본 0
          이하) 즉시 실격 · 체결가는 호가+슬리피지 0.01% · 외부 정보 활용 허용.
        </p>
        <p className="hint" style={{ margin: 0 }}>
          {state.competition_end_ms > 0 ? (
            <>
              대회 종료:{' '}
              <strong style={{ color: 'var(--text)' }}>
                {new Date(state.competition_end_ms).toLocaleString('ko-KR')}
              </strong>
              {' '}— 종료 시점 수익률이 최종 순위입니다.
            </>
          ) : (
            '대회는 첫 봇 스펙이 적용되는 순간 시작되어 4주간 진행됩니다.'
          )}
        </p>
        {!state.running && (
          <div className="banner warn" style={{ margin: 0 }}>
            봇이 정지 상태입니다 — AI 봇 규칙은 봇이 실행 중일 때만 평가됩니다.
          </div>
        )}
        {state.traders.map((t) => (
          <TraderCard key={t.name} trader={t} row={rowOf(t.name)} busy={busy} />
        ))}
      </div>
    </section>
  )
}
