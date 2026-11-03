() => {
  const present = name => document.cookie.split(';').some(x => x.trim().startsWith(name + '=') && x.trim().length > name.length + 1);
  const correctOrigin = location.origin === 'https://zhyx.eingdong.com';
  return {page_evaluation: true, fetch_available: typeof fetch === 'function',
    origin: location.origin, logged_in: correctOrigin && location.hash !== '#/login' && present('Admin-Token') && present('StoreId')};
}
