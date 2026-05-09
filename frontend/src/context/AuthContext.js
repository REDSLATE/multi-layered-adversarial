import React, { createContext, useContext, useEffect, useState } from "react";
import { api, formatApiErrorDetail, getToken, setToken } from "@/lib/api";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [status, setStatus] = useState("loading");

  useEffect(() => {
    let mounted = true;
    (async () => {
      const t = getToken();
      if (!t) {
        if (mounted) {
          setUser(null);
          setStatus("ready");
        }
        return;
      }
      try {
        const { data } = await api.get("/auth/me");
        if (mounted) setUser(data);
      } catch {
        setToken(null);
        if (mounted) setUser(null);
      } finally {
        if (mounted) setStatus("ready");
      }
    })();
    return () => {
      mounted = false;
    };
  }, []);

  async function login(email, password) {
    try {
      const { data } = await api.post("/auth/login", { email, password });
      setToken(data.access_token);
      setUser(data.user);
      return { ok: true };
    } catch (e) {
      return {
        ok: false,
        error: formatApiErrorDetail(e?.response?.data?.detail) || e.message,
      };
    }
  }

  async function logout() {
    setToken(null);
    setUser(null);
  }

  return (
    <AuthContext.Provider value={{ user, status, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
