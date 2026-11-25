import { buildSessionSlug, extractSessionIdFromSlug, slugifyGoal } from './session-slug.util';

describe('session-slug.util', () => {
  const uuid = '9ad94e2a-0ee1-4563-99fa-215bd4ba5b81';

  it('slugifies Vietnamese goals cleanly', () => {
    expect(slugifyGoal('Vào Cài đặt và đổi thời gian chờ tắt màn hình')).toBe(
      'vao-cai-dat-va-doi-thoi-gian-cho-tat-man-hinh'
    );
  });

  it('returns bare uuid for URL path segments', () => {
    expect(buildSessionSlug(uuid, 'Mở app Đồng hồ')).toBe(uuid);
    expect(buildSessionSlug(uuid, '')).toBe(uuid);
    expect(buildSessionSlug(uuid, null)).toBe(uuid);
  });

  it('extracts session id from slugged and bare inputs', () => {
    expect(extractSessionIdFromSlug(`mo-app-dong-ho--${uuid}`)).toBe(uuid);
    expect(extractSessionIdFromSlug(uuid)).toBe(uuid);
    expect(extractSessionIdFromSlug('custom-slug--active-123')).toBe('active-123');
    expect(extractSessionIdFromSlug('')).toBeNull();
  });
});

