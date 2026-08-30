import { useState } from 'react'
import { api, setToken } from '../api.js'

export default function Login({ onSuccess }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(event) {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      const { token } = await api.login(username, password)
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
        {error && <div className="banner error" style={{ marginTop: 14 }}>{error}</div>}
        <button className="primary" type="submit" disabled={busy || !username || !password}>
          {busy ? '확인 중…' : '로그인'}
        </button>
      </form>
    </div>
  )
}
