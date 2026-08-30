import { useState } from 'react'

// 자금이 움직이는 두 동작은 정확한 문구를 입력해야 실행된다.
// 백엔드도 같은 문구를 검사하므로, 이 UI 는 실수 방지용 한 겹일 뿐이다.
const CONFIRMATIONS = {
  live: { phrase: 'LIVE', label: '실거래로 시작', question: '실제 자금으로 매매를 시작합니다.' },
  close: { phrase: 'CLOSE', label: '긴급 전체 청산', question: '봇을 멈추고 보유 포지션을 전부 시장가로 청산합니다.' },
}

export default function Controls({ status, busy, onStart, onStop, onCloseAll }) {
  const [pending, setPending] = useState(null) // 'live' | 'close' | null
  const [typed, setTyped] = useState('')

  function open(kind) {
    setPending(kind)
    setTyped('')
  }

  function cancel() {
    setPending(null)
    setTyped('')
  }

  function confirm() {
    const phrase = CONFIRMATIONS[pending].phrase
    if (typed !== phrase) return
    if (pending === 'live') onStart(true, phrase)
    else onCloseAll(phrase)
    cancel()
  }

  const running = status.running
  // 설정이 깨진 상태에서는 시작 버튼을 눌러 봐야 서버가 거절한다.
  // 눌리지 않게 막고 이유를 보여 주는 편이 낫다.
  const blocked = Boolean(status.startup_error)

  return (
    <section className="panel">
      <h2>제어</h2>
      <div className="panel-body">
        <div className="controls">
          <button disabled={busy || running || blocked} onClick={() => onStart(false, '')}>
            DRY-RUN 시작
          </button>
          <button
            className="primary"
            disabled={busy || running || blocked}
            onClick={() => open('live')}
          >
            실거래 시작
          </button>
          <button disabled={busy || !running} onClick={onStop}>
            정지
          </button>
          <span className="spacer" style={{ flex: 1 }} />
          <button className="danger" disabled={busy} onClick={() => open('close')}>
            긴급 전체 청산
          </button>
        </div>

        {pending && (
          <div className="confirm-row">
            <span className="hint">
              {CONFIRMATIONS[pending].question} 계속하려면{' '}
              <strong>{CONFIRMATIONS[pending].phrase}</strong> 를 입력하세요.
            </span>
            <input
              value={typed}
              autoFocus
              placeholder={CONFIRMATIONS[pending].phrase}
              onChange={(e) => setTyped(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') confirm()
                if (e.key === 'Escape') cancel()
              }}
            />
            <button
              className={pending === 'close' ? 'danger' : 'primary'}
              disabled={typed !== CONFIRMATIONS[pending].phrase}
              onClick={confirm}
            >
              {CONFIRMATIONS[pending].label}
            </button>
            <button className="ghost" onClick={cancel}>취소</button>
          </div>
        )}

        {blocked && (
          <p className="hint" style={{ marginTop: 12, marginBottom: 0 }}>
            설정 오류가 해결될 때까지 봇을 시작할 수 없습니다.
          </p>
        )}

        {!running && !blocked && (
          <p className="hint" style={{ marginTop: 12, marginBottom: 0 }}>
            DRY-RUN 은 사이징과 규격 보정까지 전부 수행하고 주문 전송만 건너뜁니다.
            새 전략은 여기서 먼저 관찰하세요.
          </p>
        )}
      </div>
    </section>
  )
}
