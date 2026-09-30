// static/js/admin_unlock.js (v4.0 lane E): unlock admin once; the password is
// kept only in a closure for 15 minutes, then forgotten. No window.prompt (it
// shows the password in plain text) and never any storage.
const U = require('../../static/js/admin_unlock.js');

module.exports = (t) => {
    let now = 1000000;
    const u = U.createUnlock({ now: () => now });
    t.eq(U.TTL_MS, 15 * 60 * 1000);
    t.eq(u.get(), null);
    t.eq(u.isUnlocked(), false);
    t.eq(u.remainingMs(), 0);

    u.set('s3cret');
    t.eq(u.get(), 's3cret');
    t.eq(u.isUnlocked(), true);
    t.eq(u.remainingMs(), U.TTL_MS);
    now += 14 * 60 * 1000;
    t.eq(u.get(), 's3cret');                 // using it does not extend it
    t.eq(u.remainingMs(), 60 * 1000);
    t.eq(U.remainingText(u.remainingMs()), '1 min');
    t.eq(U.remainingText(14 * 60 * 1000 + 1), '15 min');
    t.eq(U.remainingText(20 * 1000), 'under a minute');
    now += 60 * 1000;
    t.eq(u.get(), null);                     // 15 minutes: gone
    t.eq(u.isUnlocked(), false);

    // an empty password is not an unlock; forget() locks at once
    u.set('');
    t.eq(u.isUnlocked(), false);
    u.set('again');
    const seen = [];
    const off = u.onChange((v) => seen.push(v));
    u.forget();
    t.eq(u.get(), null);
    t.eq(seen, [false]);
    u.set('x');
    t.eq(seen, [false, true]);
    off();
    u.forget();
    t.eq(seen, [false, true]);

    // two unlocks are independent (each page has its own closure)
    const a = U.createUnlock({ now: () => now });
    const b = U.createUnlock({ now: () => now });
    a.set('one');
    t.eq(b.get(), null);

    // the closure never leaks the password through JSON or string conversion
    a.set('topsecret');
    t.eq(JSON.stringify(a).includes('topsecret'), false);
    t.eq(String(a).includes('topsecret'), false);
};
