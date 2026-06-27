export default {
  async fetch(request) {
    const url = new URL(request.url);
    const target = "https://api.groq.com" + url.pathname + url.search;
    const init = {
      method: request.method,
      headers: request.headers,
      body: (request.method === "GET" || request.method === "HEAD") ? undefined : request.body,
    };
    const resp = await fetch(target, init);
    const out = new Response(resp.body, resp);
    out.headers.set("Access-Control-Allow-Origin", "*");
    return out;
  },
};
