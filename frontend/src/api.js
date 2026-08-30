// 백엔드 호출을 한곳에 모은다. 토큰은 sessionStorage 에만 두어 탭을 닫으면
// 사라지게 하고, 401 이 오면 즉시 로그인 화면으로 되돌린다.

const TOKEN_KEY = 'ctb.token'

export class ApiError extends Error {
  constructor(message, status) {
    super(message)
    this.status = status
  }
}

export function getToken() {
  return sessionStorage.getItem(TOKEN_KEY)
}

export function setToken(token) {
  if (token) sessionStorage.setItem(TOKEN_KEY, token)
  else sessionStorage.removeItem(TOKEN_KEY)
}

async function request(path, { method = 'GET', body, isLogin = false } = {}) {
  const headers = {}
  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`
  if (body !== undefined) headers['Content-Type'] = 'application/json'

  let response
  try {
    response = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  } catch {
    throw new ApiError('서버에 연결할 수 없습니다', 0)
  }

  // 로그인 요청의 401 은 "비밀번호가 틀렸다"는 뜻이므로 세션 만료로 바꾸지
  // 않는다. 그 외의 401 만 토큰을 비우고 로그인 화면으로 되돌린다.
  if (response.status === 401 && !isLogin) {
    setToken(null)
    throw new ApiError('세션이 만료되었습니다. 다시 로그인하세요.', 401)
  }

  let payload = null
  try {
    payload = await response.json()
  } catch {
    payload = null
  }

  if (!response.ok) {
    throw new ApiError(payload?.detail || `요청 실패 (${response.status})`, response.status)
  }
  return payload
}

export const api = {
  login: (username, password) =>
    request('/api/login', { method: 'POST', body: { username, password }, isLogin: true }),
  logout: () => request('/api/logout', { method: 'POST' }),
  status: () => request('/api/status'),
  config: () => request('/api/config'),
  positions: () => request('/api/positions'),
  logs: (since) => request(`/api/logs?since=${since}`),
  start: (live, confirm) => request('/api/bot/start', { method: 'POST', body: { live, confirm } }),
  stop: () => request('/api/bot/stop', { method: 'POST' }),
  closeAll: (confirm) =>
    request('/api/positions/close-all', { method: 'POST', body: { confirm } }),
}
