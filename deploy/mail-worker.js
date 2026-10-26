// Dikelola oleh LocalPanel: Cloudflare Email Worker
// Meneruskan email yang diterima Cloudflare Email Routing ke mail server LocalPanel lewat HTTPS (tunnel).
// Tanda tangan: HMAC-SHA256(SECRET, `${ts}\n${from}\n${to}\n` + isi email mentah), hex.
export default {
  async email(message, env) {
    const raw = new Uint8Array(await new Response(message.raw).arrayBuffer());
    const ts = Date.now().toString();
    const head = new TextEncoder().encode(`${ts}\n${message.from}\n${message.to}\n`);
    const data = new Uint8Array(head.length + raw.length);
    data.set(head);
    data.set(raw, head.length);
    const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(env.SECRET),
      { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
    const sig = [...new Uint8Array(await crypto.subtle.sign("HMAC", key, data))]
      .map((b) => b.toString(16).padStart(2, "0")).join("");
    let res;
    try {
      res = await fetch(env.ENDPOINT, {
        method: "POST", body: raw,
        headers: { "Content-Type": "message/rfc822", "X-LP-Timestamp": ts, "X-LP-From": message.from,
                   "X-LP-To": message.to, "X-LP-Signature": sig },
      });
    } catch (e) {
      throw new Error("Mail server tidak bisa dihubungi: " + e);  // Cloudflare akan mencoba lagi / bounce
    }
    if (res.status >= 400 && res.status < 500) {
      message.setReject((await res.text()).slice(0, 200) || "Alamat tidak ditemukan");
    } else if (!res.ok) {
      throw new Error("Mail server error " + res.status);
    }
  },
};
