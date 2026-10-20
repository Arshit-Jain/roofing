"use client";

import { useState } from "react";
import { createUserWithEmailAndPassword, signInWithEmailAndPassword, updateProfile } from "@firebase/auth";
import { ArrowUpRight, House } from "lucide-react";
import { auth, firebaseConfigured } from "@/lib/firebase";

export function LoginForm() {
  const [mode, setMode] = useState<"login" | "signup">("login");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    if (!auth || !firebaseConfigured) {
      setError("Set the NEXT_PUBLIC_FIREBASE_* values and start the Auth emulator to sign in.");
      return;
    }
    setBusy(true);
    try {
      if (mode === "signup") {
        const credential = await createUserWithEmailAndPassword(auth, email.trim(), password);
        if (name.trim()) await updateProfile(credential.user, { displayName: name.trim() });
      } else {
        await signInWithEmailAndPassword(auth, email.trim(), password);
      }
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : "Authentication failed.";
      setError(message.replace(/^Firebase:\s*/i, ""));
    } finally {
      setBusy(false);
    }
  }

  return <main className="auth-page">
    <section className="auth-aside">
      <div><div className="brand"><span className="brand-mark"><House size={18} /></span><span>ROOFLINE</span></div>
        <div><div className="eyebrow" style={{ color: "#bce2d2", marginTop: 46 }}>Your estimating workspace</div>
          <h1>Build a better roof quote.</h1>
          <p>Review measurements, price materials, and share a clear estimate from one calm, practical workspace.</p>
        </div>
      </div>
      <div className="auth-foot" style={{ color: "#bbcec7", fontSize: 12 }}>Made for the details above the ceiling.</div>
    </section>
    <section className="auth-card-wrap">
      <form className="auth-card" onSubmit={submit}>
        <div className="eyebrow">Contractor sign in</div>
        <h2>{mode === "login" ? "Welcome back" : "Create your account"}</h2>
        <p className="subcopy">Access your projects and estimates securely.</p>
        {!firebaseConfigured && <div className="notice" role="status">Add the public Firebase Web app settings to `apps/web/.env.local` before signing in.</div>}
        {error && <div className="error" role="alert">{error}</div>}
        {mode === "signup" && <div className="field"><label htmlFor="name">Name</label><input id="name" className="input" autoComplete="name" value={name} onChange={(event) => setName(event.target.value)} required /></div>}
        <div className="field"><label htmlFor="email">Email</label><input id="email" className="input" type="email" autoComplete="email" value={email} onChange={(event) => setEmail(event.target.value)} required /></div>
        <div className="field"><label htmlFor="password">Password</label><input id="password" className="input" type="password" autoComplete={mode === "login" ? "current-password" : "new-password"} minLength={6} value={password} onChange={(event) => setPassword(event.target.value)} required /></div>
        <button className="button" type="submit" disabled={busy} style={{ width: "100%", marginTop: 6 }}>{busy ? "Please wait…" : mode === "login" ? "Sign in" : "Create account"}<ArrowUpRight size={16} /></button>
        <p className="subcopy" style={{ textAlign: "center", marginTop: 22 }}>{mode === "login" ? "New to Roofline?" : "Already have an account?"}{" "}
          <button type="button" className="button ghost small" onClick={() => { setMode(mode === "login" ? "signup" : "login"); setError(""); }}>{mode === "login" ? "Create account" : "Sign in"}</button>
        </p>
      </form>
    </section>
  </main>;
}
