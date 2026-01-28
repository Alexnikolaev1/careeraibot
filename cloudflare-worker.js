export default {
  async fetch(request) {
    const url = new URL(request.url);
    
    // Обработка корневого пути
    if (url.pathname === "/" || url.pathname === "") {
      return new Response(JSON.stringify({status: "ok", message: "Gemini API Proxy"}), {
        headers: {"Content-Type": "application/json"},
      });
    }

    // CORS preflight
    if (request.method === "OPTIONS") {
      return new Response(null, {
        headers: {
          "Access-Control-Allow-Origin": "*",
          "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
          "Access-Control-Allow-Headers": "*",
        },
      });
    }

    const externalUrl = "https://generativelanguage.googleapis.com";
    const proxiedUrl = externalUrl + url.pathname + url.search;

    try {
      const proxiedRequest = new Request(proxiedUrl, request);
      const response = await fetch(proxiedRequest);
      const newResponse = new Response(response.body, response);
      newResponse.headers.set("Access-Control-Allow-Origin", "*");
      return newResponse;
    } catch (error) {
      return new Response(JSON.stringify({error: error.message}), {
        status: 500,
        headers: {"Content-Type": "application/json"},
      });
    }
  },
};