import type enAuth from '../en/auth';

export default {
  'login': '登录',
  'logging_in': '登录中...',
  'login_failed': '登录失败',
  'username': '用户名',
  'password': '密码',
  'login_subtitle': '使用账号登录',
  'login_with_newapi': '用 new-api 登录',
  'sso_signing_in': '正在登录…',
  'sso_state_mismatch': '登录请求已失效，请回到登录页重新发起',
  'back_to_login': '返回登录',
} satisfies Record<keyof typeof enAuth, string>;
