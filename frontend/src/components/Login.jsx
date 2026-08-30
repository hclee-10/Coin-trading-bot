import { useEffect, useState } from 'react'
import { api, setToken } from '../api.js'

export default function Login({ onSuccess }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [code, setCode] = useState('')
  const [needsCode, setNeedsCode] = useState(false)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  // 2단계 인증을 쓰는지 먼저 물어본다. 모르면 빈 칸 앞에서 헤매게 된다.
  useEffect(() => {
    api
      .loginOptions()
      .then((options) => setNeedsCode(Boolean(options.totp_required)))
      .catch(() => setNeedsCode(false))
  }, [])

  async function submit(event) {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      const { token } = await api.login(username, password, code)
      setToken(token)
      setPassword('')
      setCode('')
      onSuccess()
    } catch (err) {
      setError(err.message)
      setCode('')   // 코드는 한 번 쓰면 끝이다 — 다음 코드를 새로 받아야 한다
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-wrap">
      <form className="login" onSubmit={submit}>
        <h1>Coin Trading Bot</h1>
        <p>대시보드에 접속하려면 아이디와 비밀번호를 입력하세요.</p>
        <label htmlFor="username">아이디</label>
        <input
          id="username"
          name="username"
          type="text"
          value={username}
          autoFocus
          autoComplete="username"
          onChange={(e) => setUsername(e.target.value)}
        />
        <label htmlFor="password" style={{ marginTop: 14 }}>비밀번호</label>
        <input
          id="password"
          name="password"
          type="password"
          value={password}
          autoComplete="current-password"
          onChange={(e) => setPassword(e.target.value)}
        />
        {needsCode && (
          <>
            <label htmlFor="code" style={{ marginTop: 14 }}>인증 코드</label>
            <input
              id="code"
              name="one-time-code"
              type="text"
              inputMode="numeric"
              autoComplete="one-time-code"
              placeholder="000000"
              maxLength={6}
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))}
            />
            <p className="hint" style={{ margin: '6px 0 0' }}>
              인증 앱에 표시된 6자리 숫자
            </p>
          </>
        )}
        {error && <div className="banner error" style={{ marginTop: 14 }}>{error}</div>}
        <button
          className="primary"
          type="submit"
          disabled={busy || !username || !password || (needsCode && code.length !== 6)}
        >
          {busy ? '확인 중…' : '로그인'}
        </button>
      </form>
    </div>
  )
}
