// Shows the push the server sends when the bot needs the user, and opens the chat when it is tapped.
'use strict';

self.addEventListener('push', (event) => {
  const data = event.data ? event.data.json() : { title: 'Monty', body: 'Monty needs you.', url: '/' };
  event.waitUntil(self.registration.showNotification(data.title, { body: data.body, data: { url: data.url }, tag: data.tag }));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  event.waitUntil(self.clients.openWindow(event.notification.data.url || '/'));
});
