import { useState } from "react";

import { StarLogo } from "../icons/StarLogo";
import { ThemeToggle } from "../common/ThemeToggle";
import { actions } from "../../state/store";
import { ApiError } from "../../api/client";

type AuthMode = "login" | "register";

/** 登录 / 注册视图（#0026 T9）：手机号 + 密码（注册带短信验证码），claude.ai 观感。 */
export function AuthView() {
  const [mode, setMode] = useState<AuthMode>("login");
  const [phone, setPhone] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [msg, setMsg] = useState<{ text: string; ok: boolean } | null>(null);
  const [busy, setBusy] = useState(false);

  const isRegister = mode === "register";

  const sendCode = async () => {
    setMsg(null);
    try {
      const r = await actions.sendCode(phone);
      if (r.code) {
        setCode(r.code); // dev 打桩：自动填入
        setMsg({ text: "验证码已发送（dev：已自动填入）", ok: true });
      } else {
        setMsg({ text: "验证码已发送", ok: true });
      }
    } catch (e) {
      setMsg({ text: e instanceof ApiError ? e.detail : "发送失败", ok: false });
    }
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setMsg(null);
    try {
      if (isRegister) {
        await actions.register(phone, code, password);
        setMsg({ text: "注册成功，正在登录…", ok: true });
      }
      await actions.login(phone, password);
      // 成功后 App 据 auth 状态切到工作区（本组件随之卸载）。
    } catch (err) {
      setMsg({
        text: err instanceof ApiError ? err.detail : isRegister ? "注册失败" : "登录失败",
        ok: false,
      });
    } finally {
      setBusy(false);
    }
  };

  const toggle = () => {
    setMode((m) => (m === "login" ? "register" : "login"));
    setMsg(null);
  };

  return (
    <div className="auth-screen">
      <div className="auth-screen__theme">
        <ThemeToggle />
      </div>
      <form className="auth-card" onSubmit={submit}>
        <StarLogo size={34} className="auth-card__logo" />
        <h1 className="auth-card__title serif">{isRegister ? "Create your account" : "Welcome back"}</h1>
        <p className="auth-card__sub">
          {isRegister ? "手机号 → 短信验证码 → 设密码" : "手机号 + 密码登录"}
        </p>

        <label className="sr-only" htmlFor="auth-phone">
          手机号
        </label>
        <input
          id="auth-phone"
          className="auth-input"
          placeholder="手机号（11 位）"
          autoComplete="username"
          value={phone}
          onChange={(e) => setPhone(e.target.value)}
        />

        {isRegister && (
          <div className="auth-row">
            <label className="sr-only" htmlFor="auth-code">
              短信验证码
            </label>
            <input
              id="auth-code"
              className="auth-input"
              placeholder="短信验证码"
              value={code}
              onChange={(e) => setCode(e.target.value)}
            />
            <button type="button" className="btn btn--ghost auth-code-btn" onClick={sendCode}>
              获取验证码
            </button>
          </div>
        )}

        <label className="sr-only" htmlFor="auth-pw">
          密码
        </label>
        <input
          id="auth-pw"
          className="auth-input"
          type="password"
          placeholder="密码（≥8 位）"
          autoComplete={isRegister ? "new-password" : "current-password"}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />

        {msg && <div className={`auth-msg${msg.ok ? " is-ok" : " is-err"}`}>{msg.text}</div>}

        <button type="submit" className="btn btn--primary auth-submit" disabled={busy}>
          {isRegister ? "注册" : "登录"}
        </button>

        <button type="button" className="auth-toggle" onClick={toggle}>
          {isRegister ? "已有账号？去登录" : "没有账号？去注册"}
        </button>
      </form>
    </div>
  );
}
