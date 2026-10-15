import type enAuth from "@/i18n/en/auth";

export default {
  'login': 'Đăng nhập',
  'logging_in': 'Đang đăng nhập...',
  'login_failed': 'Đăng nhập thất bại',
  'username': 'Tên đăng nhập',
  'password': 'Mật khẩu',
  'login_subtitle': 'Đăng nhập bằng tài khoản của bạn',
  'login_with_newapi': 'Đăng nhập bằng new-api',
  'sso_signing_in': 'Đang đăng nhập…',
  'sso_state_mismatch': 'Yêu cầu đăng nhập đã hết hiệu lực, vui lòng quay lại trang đăng nhập và thử lại',
  'back_to_login': 'Quay lại đăng nhập',
} satisfies Record<keyof typeof enAuth, string>;
