import { useState } from 'react'
import { api } from '../api.js'

// AI 경쟁 매매 — 클로드/GPT/그록의 판단을 웹으로 전달받아 모의매매로 체결한다.
// 흐름: 프롬프트 복사 → AI 웹에 붙여넣기 → 답(롱/숏/청산)을 여기 입력 → 15초 내 체결.

function signed(value, digits = 2) {
  if (value === null || value === undefined) return '—'
  return `${value >= 0 ? '+' : ''}${value.toFixed(digits)}`
}

function TraderCard({ trader, row, running, busy, onOrder }) {
  const [action, setAction] = useState('long')
  const [amount, setAmount] = useState('100')
  const [note, setNote] = useState('')
  const [promptState, setPromptState] = useState('')  // '' | 'loading' | 'copied' | 'manual'
  const [promptText, setPromptText] = useState('')

  const submit = async () => {
    const value = Number(amount)
    if (action !== 'close' && (!Number.isFinite(value) || value <= 0)) {
      setNote('금액을 확인하세요')
      return
    }
    try {
      await onOrder(trader.name, action, action === 'close' ? 0 : value)
      setNote(running ? '입력됨 — 다음 주기(15초)에 체결' : '입력됨 — 봇을 시작하면 체결됩니다')
    } catch {
      setNote('')
    }
  }

  const copyPrompt = async () => {
    setPromptState('loading')
    try {
      const { prompt } = await api.aiPrompt(trader.name)
      setPromptText(prompt)
      try {
        await navigator.clipboard.writeText(prompt)
        setPromptState('copied')
      } catch {
        setPromptState('manual')  // 클립보드 권한이 없으면 아래 텍스트를 직접 복사
      }
    } catch {
      setPromptState('')
      setNote('프롬프트 생성 실패')
    }
  }

  return (
    <div style={{ border: '1px solid var(--border)', borderRadius: 8, padding: '12px 14px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
        <strong>{trader.label}</strong>
        {row ? (
          <span className="hint">
            수익률 <span className={row.return_pct >= 0 ? 'pos' : 'neg'}>
              {signed(row.return_pct)}%
            </span>
            {row.position_amount > 0 && (
              <>
                {' · '}
                <span className={row.position_side === 'long' ? 'pos' : 'neg'}>
                  {row.position_side === 'long' ? '롱' : '숏'}
                </span>
                {` ${row.position_notional.toFixed(0)}$ @ ${row.position_entry.toLocaleString(undefined, { maximumFractionDigits: 1 })}`}
                {' · 평가 '}
                <span className={row.unrealized >= 0 ? 'pos' : 'neg'}>{signed(row.unrealized)}</span>
              </>
            )}
            {row.position_amount === 0 && ' · 포지션 없음'}
          </span>
        ) : (
          <span className="hint">아직 기록 없음</span>
        )}
        <span className="spacer" />
        <button className="ghost" style={{ fontSize: 12 }} onClick={copyPrompt}
                disabled={promptState === 'loading'}>
          {promptState === 'loading' ? '생성 중…'
            : promptState === 'copied' ? '✓ 복사됨'
            : '프롬프트 복사'}
        </button>
      </div>

      {trader.pending.length > 0 && (
        <div className="hint" style={{ marginTop: 8 }}>
          대기 중 주문 {trader.pending.length}건:{' '}
          {trader.pending.map((o, i) => (
            <span key={i}>
              {i > 0 && ', '}
              {o.action === 'close' ? '청산' : `${o.action === 'long' ? '롱' : '숏'} ${o.notional.toFixed(0)}$`}
            </span>
          ))}
        </div>
      )}

      <div className="confirm-row" style={{ marginTop: 10 }}>
        <select value={action} onChange={(e) => setAction(e.target.value)}>
          <option value="long">롱</option>
          <option value="short">숏</option>
          <option value="close">전량 청산</option>
        </select>
        {action !== 'close' && (
          <input
            type="number" min="1" max="100000" step="1"
            value={amount}
            onChange={(e) => setAmount(e.target.value)}
            style={{ width: 110 }}
            placeholder="USDT"
          />
        )}
        <button disabled={busy} onClick={submit}>주문 입력</button>
        {note && <span className="hint">{note}</span>}
      </div>

      {promptText && promptState !== '' && (
        <details open={promptState === 'manual'} style={{ marginTop: 8 }}>
          <summary className="hint" style={{ cursor: 'pointer' }}>
            {promptState === 'manual' ? '복사 권한이 없어요 — 아래 내용을 직접 복사하세요' : '프롬프트 내용 보기'}
          </summary>
          <textarea
            readOnly value={promptText}
            style={{ width: '100%', minHeight: 160, marginTop: 6, fontSize: 12 }}
            onFocus={(e) => e.target.select()}
          />
        </details>
      )}
    </div>
  )
}

export default function AITraders({ state, leaderboard, busy, onOrder }) {
  if (!state) return null
  const rowOf = (name) => leaderboard?.strategies?.find((s) => s.name === name)

  return (
    <section className="panel">
      <h2>
        AI 경쟁 매매
        <span className="spacer" />
        <span className="hint">클로드 · GPT · 그록 — 같은 질문, 다른 판단</span>
      </h2>
      <div className="panel-body" style={{ display: 'grid', gap: 12 }}>
        <p className="hint" style={{ margin: 0 }}>
          ① <strong>프롬프트 복사</strong>를 눌러 현재 시세·계좌 상태가 담긴 질문지를 만들고
          ② 각 AI 의 웹 채팅에 붙여넣은 뒤 ③ 답(예: “숏 300달러”)을 아래에 입력하면
          다음 주기(15초)에 그 AI 의 가상 계좌(1만 USDT)로 체결됩니다. 성적은 위의
          전략 순위표에서 알고리즘 전략들과 함께 비교됩니다.
        </p>
        {!state.running && (
          <div className="banner warn" style={{ margin: 0 }}>
            봇이 정지 상태입니다 — 주문은 대기만 되고, 봇을 시작해야 체결됩니다.
          </div>
        )}
        {state.traders.map((t) => (
          <TraderCard
            key={t.name}
            trader={t}
            row={rowOf(t.name)}
            running={state.running}
            busy={busy}
            onOrder={onOrder}
          />
        ))}
      </div>
    </section>
  )
}
