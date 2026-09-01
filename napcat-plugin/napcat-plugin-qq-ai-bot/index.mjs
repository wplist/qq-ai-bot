// QQ AI 机器人（GLM）配置控制台 —— NapCat 插件
//
// 职责：
// 1) 在 NapCat WebUI 侧边栏注册控制台页面（唯一管理入口）
// 2) 注册同源 API 路由（/plugin/napcat-plugin-qq-ai-bot/api/*），
//    读取本目录下 api.port 文件，把请求转发给机器人程序的内部随机端口。
//    浏览器全程只与 6099 同源通信，无跨域、无额外固定端口。

import http from "node:http";
import path from "node:path";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

const PLUGIN_DIR = path.dirname(fileURLToPath(import.meta.url));
const PORT_FILE = path.join(PLUGIN_DIR, "api.port");

async function readBotPort() {
  try {
    const p = parseInt((await readFile(PORT_FILE, "utf-8")).trim(), 10);
    return p > 0 ? p : null;
  } catch {
    return null;
  }
}

async function readBody(req) {
  // 优先用中间件已解析的 req.body；否则尝试按流读取（环境差异都要兼容）
  if (req.body !== undefined && req.body !== null) {
    return Buffer.from(JSON.stringify(req.body));
  }
  if (typeof req.on !== "function") {
    return undefined;
  }
  return await new Promise((resolve) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => resolve(chunks.length ? Buffer.concat(chunks) : undefined));
    req.on("error", () => resolve(undefined));
  });
}

async function proxy(req, res, apiPath) {
  const port = await readBotPort();
  if (!port) {
    res.status(503).json({ ok: false, message: "机器人程序未运行（找不到 api.port），请先在启动器里启动机器人" });
    return;
  }
  const body = ["POST", "PUT"].includes(req.method) ? await readBody(req) : undefined;
  const upstream = http.request(
    {
      host: "127.0.0.1",
      port,
      path: apiPath,
      method: req.method,
      headers: body
        ? { "Content-Type": "application/json", "Content-Length": body.length }
        : {},
    },
    (up) => {
      const out = [];
      up.on("data", (c) => out.push(c));
      up.on("end", () => {
        const text = Buffer.concat(out).toString("utf-8");
        try {
          res.status(up.statusCode || 200).json(JSON.parse(text));
        } catch {
          res.status(up.statusCode || 502).json({ ok: false, message: text.slice(0, 200) });
        }
      });
    }
  );
  upstream.on("error", (e) => {
    res.status(502).json({ ok: false, message: "连接机器人失败：" + e.message });
  });
  if (body) upstream.write(body);
  upstream.end();
}

export const plugin_init = async (ctx) => {
  ctx.logger.info("[QQ AI 机器人] 插件已加载（控制台页面 + 同源API转发）");
  ctx.router.page({
    title: "QQ AI 机器人",
    path: "qq-ai-bot",
    htmlFile: "webui/index.html",
    icon: "🤖",
    description: "机器人配置控制台：NapCat 接口 / API Key / 管理员绑定 / 状态测试",
  });
  ctx.router.getNoAuth("/status", (req, res) => proxy(req, res, "/api/status"));
  ctx.router.postNoAuth("/settings", (req, res) => proxy(req, res, "/api/settings"));
  ctx.router.postNoAuth("/test-glm", (req, res) => proxy(req, res, "/api/test-glm"));
  ctx.router.postNoAuth("/test-napcat", (req, res) => proxy(req, res, "/api/test-napcat"));
};
