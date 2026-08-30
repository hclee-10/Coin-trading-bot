import { useCallback, useEffect, useRef, useState } from 'react'
import { api, getToken, setToken } from './api.js'
import Login from './components/Login.jsx'
import StatusCards from './components/StatusCards.jsx'
import Controls from './components/Controls.jsx'
import Settings from './components/Settings.jsx'
import Positions from './components/Positions.jsx'
import Chart from './components/Chart.jsx'
import Performance from './components/Performance.jsx'
import StrategyInfo from './components/StrategyInfo.jsx'
import Leaderboard from './components/Leaderboard.jsx'
import Logs from './components/Logs.jsx'

const POLL_MS = 2000
const MAX_LOG_LINES = 500

// 지금 실행 중인 번들 파일명. 서버가 서빙하는 것과 다르면 이 화면이 낡은 것이다.
// 재배포한 뒤 브라우저가 예전 화면을 캐시해 "왜 안 바뀌지" 로 헤매는 일을 막는다.
const RUNNING_BUNDLE = import.meta.url.split('/').pop()

export default function App() {
  const [authed, setAuthed] = useState(Boolean(getToken()))
  const [status, setStatus] = useState(null)
  const [positions, setPositions] = useState({ source: null, positions: [] })
  const [chart, setChart] = useState(null)
  const [performance, setPerformance] = useState(null)
  const [catalog, setCatalog] = useState(null)
  const [config, setConfig] = useState(null)
  const [stale, setStale] = useState(false)
  const [leaderboard, setLeaderboard] = useState(null)
  const [logs, setLogs] = useState([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const logSeq = useRef(0)

  const handleError = useCallback((err) => {
    if (err.status === 401) {
      setAuthed(false)
      setStatus(null)
    }
    setError(err.message)
  }, [])

  // 상태·포지션·로그를 한 주기에 함께 갱신한다. 서버는 캐시된 값을 돌려주므로
  // 이 폴링이 거래소에 부하를 주지는 않는다.
  //
  // 세 요청을 개별적으로 처리하는 것이 중요하다. 하나로 묶으면 포지션 조회가
  // 잠깐 실패했다는 이유로 상태 카드와 정지·긴급청산 버튼까지 화면에서
  // 사라진다 — 네트워크가 흔들릴 때가 바로 그 버튼이 가장 필요한 때다.
  const applyLogs = useCallback(({ entries, latest_seq: latestSeq }) => {
    // 서버가 재시작하면 로그 시퀀스가 초기화된다. 우리가 들고 있던 번호가
    // 서버보다 크면 그런 경우다 — 그대로 두면 "그 번호 이후" 를 계속 요청해
    // 아무것도 못 받고 화면의 로그가 영원히 멈춘다.
    if (latestSeq < logSeq.current) {
      logSeq.current = 0
      setLogs([])
      return
    }
    if (entries.length > 0) {
      logSeq.current = latestSeq
      setLogs((prev) => [...prev, ...entries].slice(-MAX_LOG_LINES))
    }
  }, [])

  const poll = useCallback(async () => {
    // 각 응답이 도착하는 대로 화면에 반영한다. 전부 모아서 한 번에 그리면,
    // 봇이 멈춰 있을 때 차트가 거래소에서 캔들을 받아오는 몇 초 동안 화면
    // 전체가 "불러오는 중" 에 묶인다 — 상태는 이미 도착해 있는데도.
    const results = await Promise.allSettled([
      api.status().then(setStatus),
      api.positions().then(setPositions),
      api.logs(logSeq.current).then(applyLogs),
      api.chart().then(setChart),
      api.performance().then(setPerformance),
      api.leaderboard().then(setLeaderboard),
    ])

    const failures = results.filter((r) => r.status === 'rejected')
    const expired = failures.find((r) => r.reason?.status === 401)
    if (expired) {
      handleError(expired.reason)
      return
    }
    setError(failures.length > 0 ? failures[0].reason.message : '')
  }, [handleError, applyLogs])

  useEffect(() => {
    if (!authed) return undefined
    // 전략 목록과 거래 설정은 프로세스가 사는 동안 바뀌지 않으므로 한 번만 받는다
    api.strategies().then(setCatalog).catch(() => setCatalog(null))
    api.config().then(setConfig).catch(() => setConfig(null))
    api
      .build()
      .then(({ bundle }) => setStale(Boolean(bundle) && bundle !== RUNNING_BUNDLE))
      .catch(() => setStale(false))
    poll()
    const timer = setInterval(poll, POLL_MS)
    return () => clearInterval(timer)
  }, [authed, poll])

  async function act(fn) {
    setBusy(true)
    setError('')
    try {
      await fn()
      await poll()
    } catch (err) {
      handleError(err)
    } finally {
      setBusy(false)
    }
  }

  async function logout() {
    try {
      await api.logout()
    } catch {
      // 이미 만료된 토큰이면 무시하고 로그인 화면으로 보낸다
    }
    setToken(null)
    setAuthed(false)
    setStatus(null)
    setLogs([])
    logSeq.current = 0
  }

  if (!authed) {
    return <Login onSuccess={() => { setError(''); setAuthed(true) }} />
  }

  if (!status) {
    return (
      <div className="app">
        {error ? <div className="banner error">{error}</div> : <p className="hint">불러오는 중…</p>}
      </div>
    )
  }

  return (
    <div className="app">
      <header className="header">
        <h1>Coin Trading Bot</h1>
        <span className={`badge ${status.running ? 'on' : 'off'}`}>
          <span className="dot" />
          {status.running ? '실행 중' : '정지됨'}
        </span>
        {status.running && (
          <span className={`badge ${status.live ? 'live' : 'dry'}`}>
            {status.live ? '실거래' : 'DRY-RUN'}
          </span>
        )}
        <span className="spacer" />
        <span className="meta">
          {status.exchange.toUpperCase()} · {status.leverage}x · {status.symbols.join(', ')}
          {' · '}
          <strong style={{ color: 'var(--text)' }}>{status.strategy}</strong>
        </span>
        <button className="ghost" onClick={logout}>로그아웃</button>
      </header>

      {stale && (
        <div className="banner warn">
          <strong>이 화면은 예전 버전입니다.</strong>
          서버에는 새 버전이 올라와 있습니다. 새로고침(Ctrl+Shift+R)하세요.
        </div>
      )}

      {status.startup_error && (
        <div className="banner error">
          <strong>설정에 문제가 있어 봇을 시작할 수 없습니다.</strong>
          {status.startup_error}
          <br />
          Railway 의 Variables 를 고친 뒤 다시 배포하세요.
        </div>
      )}

      {status.live && status.running && (
        <div className="banner live">
          <strong>실거래 모드로 동작 중입니다.</strong>
          실제 자금으로 주문이 전송됩니다.
        </div>
      )}

      {status.halted && (
        <div className="banner warn">
          <strong>킬스위치 작동 중 — 신규 진입이 차단되었습니다.</strong>
          {status.halt_reason} (UTC 일자가 바뀌면 자동 해제됩니다)
        </div>
      )}

      {error && <div className="banner error">{error}</div>}
      {status.last_error && !error && (
        <div className="banner warn">
          <strong>마지막 주기에서 오류가 발생했습니다.</strong>
          {status.last_error}
        </div>
      )}

      <StatusCards status={status} />

      <Controls
        status={status}
        busy={busy}
        onStart={(live, confirm) => act(() => api.start(live, confirm))}
        onStop={() => act(() => api.stop())}
        onCloseAll={(confirm) => act(() => api.closeAll(confirm))}
      />

      <Settings config={config} equity={status.equity} />

      <Chart
        symbol={chart?.symbol || status.symbols[0]}
        timeframe={chart?.timeframe || status.timeframe}
        candles={chart?.candles}
        markers={chart?.markers}
      />

      <Leaderboard
        data={leaderboard}
        catalog={catalog}
        busy={busy}
        onReset={(confirm) => act(() => api.resetLeaderboard(confirm))}
        storage={status.storage}
      />

      <StrategyInfo catalog={catalog} />

      <Positions positions={positions.positions} source={positions.source} />

      <Performance
        performance={performance}
        currency={status.quote_currency}
        storage={status.storage}
      />

      <Logs entries={logs} />
    </div>
  )
}
