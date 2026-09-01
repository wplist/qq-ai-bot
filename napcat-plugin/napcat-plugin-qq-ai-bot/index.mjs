// QQ AI 机器人（GLM）配置控制台 —— NapCat 插件
// 在 NapCat WebUI 侧边栏注册控制台页面，页面数据来自本机机器人程序的
// 管理 API（默认 http://127.0.0.1:8080）。

export const plugin_init = async (ctx) => {
  ctx.logger.info("[QQ AI 机器人] 插件已加载，控制台页面已注册");
  ctx.router.page({
    title: "QQ AI 机器人",
    path: "qq-ai-bot",
    htmlFile: "webui/index.html",
    icon: "🤖",
    description: "机器人配置控制台：NapCat 接口 / API Key / 管理员绑定 / 状态测试",
  });
};
