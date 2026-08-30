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

        {/* 정지 버튼을 눌러도 15초 뒤 되살아나면 버그로 보인다 — 그렇지 않다는
            것과, 반대로 자동 재시작이 꺼졌다는 것을 둘 다 알려 준다. */}
        <p className="hint" style={{ marginTop: 12, marginBottom: 0 }}>
          {status.auto_restart ? (
            <>
              <strong style={{ color: 'var(--green)' }}>자동 재시작 켜짐</strong> — 재배포하거나
              봇이 죽어도 {status.auto_restart_live ? '실거래' : 'DRY-RUN'} 모드로 다시 켜집니다.
              {status.auto_restart_count > 0 && ` (자동 시작 ${status.auto_restart_count}회)`}
              {' '}정지 버튼을 누르면 꺼집니다.
            </>
          ) : (
            <>
              <strong className="warn-text">자동 재시작 꺼짐</strong> — 지금 멈추면 재배포 전까지
              모의매매 기록이 쌓이지 않습니다. 다시 켜려면 시작 버튼을 누르세요.
            </>
          )}
        </p>
      </div>
    </section>
  )
}
