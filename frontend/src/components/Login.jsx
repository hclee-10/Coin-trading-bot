import { useState } from 'react'
import { api, setToken } from '../api.js'

export default function Login({ onSuccess }) {
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(event) {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      const { token } = await api.login(password)
      setToken(token)
      setPassword('')
      onSuccess()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-wrap">
      <form className="login" onSubmit={submit}>
        <h1>Coin Trading Bot</h1>
        <p>대시보드에 접속하려면 비밀번호를 입력하세요.</p>
        {/* 비밀번호 관리자가 항목을 저장할 수 있도록 하는 숨은 사용자명 필드 */}
        <input
          type="text"
          name="username"
          autoComplete="username"
          value="dashboard"
          readOnly
          hidden
        />
        <label htmlFor="password">비밀번호</label>
        <input
          id="password"
          type="password"
          value={password}
          autoFocus
          autoComplete="current-password"
          onChange={(e) => setPassword(e.target.value)}
        />
        {error && <div className="banner error" style={{ marginTop: 14 }}>{error}</div>}
        <button className="primary" type="submit" disabled={busy || !password}>
          {busy ? '확인 중…' : '로그인'}
        </button>
      </form>
    </div>
  )
}
