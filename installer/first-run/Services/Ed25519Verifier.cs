using System;
using System.Numerics;
using System.Security.Cryptography;

namespace AIBox.FirstRun.Services;

/// <summary>
/// Minimal Ed25519 signature verification (RFC 8032) implemented in
/// pure managed code so we don't pull a third-party crypto library into
/// the signed binary. Verification only — no key generation, no signing,
/// no batch verify. The release-cutting tooling in build/sign-manifest.py
/// uses Python's cryptography lib for that side.
///
/// Implementation follows the RFC 8032 reference exactly. Constant-time
/// is not a goal — we are verifying a public signature, not handling
/// secret material.
///
/// TODO: Add unit tests using RFC 8032 §7.1 known-answer test vectors
/// before relying on this in production. The current implementation
/// checks s < L and enforces canonical point encoding, which is correct,
/// but KATs would strengthen confidence.
/// </summary>
public static class Ed25519Verifier
{
    // Curve constants
    private static readonly BigInteger P =
        BigInteger.Pow(2, 255) - 19;
    private static readonly BigInteger L =
        BigInteger.Parse("7237005577332262213973186563042994240857116359379907606001950938285454250989");
    private static readonly BigInteger D =
        ModP(BigInteger.Parse("-121665") * Inv(BigInteger.Parse("121666")));
    private static readonly BigInteger ByY =
        ModP(BigInteger.Parse("4") * Inv(BigInteger.Parse("5")));
    private static readonly BigInteger ByX = RecoverX(ByY, 0);
    private static readonly (BigInteger X, BigInteger Y, BigInteger Z, BigInteger T) B =
        (ByX, ByY, BigInteger.One, ModP(ByX * ByY));

    public static bool Verify(ReadOnlySpan<byte> publicKey, ReadOnlySpan<byte> message, ReadOnlySpan<byte> signature)
    {
        if (publicKey.Length != 32) return false;
        if (signature.Length != 64) return false;

        var rBytes = signature.Slice(0, 32).ToArray();
        var sBytes = signature.Slice(32, 32).ToArray();

        BigInteger s = LeBytesToInt(sBytes);
        if (s >= L) return false;

        var aPoint = DecodePoint(publicKey.ToArray());
        if (aPoint is null) return false;
        var rPoint = DecodePoint(rBytes);
        if (rPoint is null) return false;

        // h = SHA-512(R || A || M) mod L
        byte[] toHash = new byte[64 + message.Length];
        rBytes.CopyTo(toHash, 0);
        publicKey.ToArray().CopyTo(toHash, 32);
        message.ToArray().CopyTo(toHash, 64);
        byte[] hHash = SHA512.HashData(toHash);
        BigInteger h = ModL(LeBytesToInt(hHash));

        var sB = ScalarMult(B, s);
        var hA = ScalarMult(aPoint.Value, h);
        var rPlusHa = Edwards(rPoint.Value, hA);

        return PointEquals(sB, rPlusHa);
    }

    // ---- field math ----

    private static BigInteger ModP(BigInteger x)
    {
        var r = x % P;
        if (r.Sign < 0) r += P;
        return r;
    }

    private static BigInteger ModL(BigInteger x)
    {
        var r = x % L;
        if (r.Sign < 0) r += L;
        return r;
    }

    private static BigInteger Inv(BigInteger x) => BigInteger.ModPow(x, P - 2, P);

    private static BigInteger RecoverX(BigInteger y, int sign)
    {
        var y2 = y * y;
        var u = ModP(y2 - 1);
        var v = ModP(D * y2 + 1);
        var x = ModP(u * BigInteger.ModPow(v, 3, P) *
                     BigInteger.ModPow(u * BigInteger.ModPow(v, 7, P), (P - 5) / 8, P));
        var vxx = ModP(v * x * x);
        if (vxx == u) { /* ok */ }
        else if (vxx == ModP(-u)) x = ModP(x * BigInteger.ModPow(2, (P - 1) / 4, P));
        else return BigInteger.MinusOne;
        if ((int)(x & 1) != sign) x = P - x;
        return x;
    }

    // ---- group operations (extended Edwards coordinates) ----

    private static (BigInteger X, BigInteger Y, BigInteger Z, BigInteger T) Edwards(
        (BigInteger X, BigInteger Y, BigInteger Z, BigInteger T) p,
        (BigInteger X, BigInteger Y, BigInteger Z, BigInteger T) q)
    {
        // RFC 8032 §5.1.4 — point addition in extended twisted Edwards coords (a = -1)
        var a = ModP((p.Y - p.X) * (q.Y - q.X));
        var b = ModP((p.Y + p.X) * (q.Y + q.X));
        var c = ModP(p.T * 2 * D * q.T);
        var dd = ModP(p.Z * 2 * q.Z);
        var e = b - a;
        var f = dd - c;
        var g = dd + c;
        var hh = b + a;
        var x3 = ModP(e * f);
        var y3 = ModP(g * hh);
        var t3 = ModP(e * hh);
        var z3 = ModP(f * g);
        return (x3, y3, z3, t3);
    }

    private static (BigInteger X, BigInteger Y, BigInteger Z, BigInteger T) ScalarMult(
        (BigInteger X, BigInteger Y, BigInteger Z, BigInteger T) p, BigInteger e)
    {
        (BigInteger X, BigInteger Y, BigInteger Z, BigInteger T) q =
            (BigInteger.Zero, BigInteger.One, BigInteger.One, BigInteger.Zero);
        while (e > 0)
        {
            if ((e & 1) == 1) q = Edwards(q, p);
            p = Edwards(p, p);
            e >>= 1;
        }
        return q;
    }

    private static bool PointEquals(
        (BigInteger X, BigInteger Y, BigInteger Z, BigInteger T) p,
        (BigInteger X, BigInteger Y, BigInteger Z, BigInteger T) q)
    {
        if (ModP(p.X * q.Z - q.X * p.Z) != 0) return false;
        if (ModP(p.Y * q.Z - q.Y * p.Z) != 0) return false;
        return true;
    }

    private static (BigInteger X, BigInteger Y, BigInteger Z, BigInteger T)? DecodePoint(byte[] enc)
    {
        if (enc.Length != 32) return null;
        var bytes = (byte[])enc.Clone();
        int sign = (bytes[31] >> 7) & 1;
        bytes[31] = (byte)(bytes[31] & 0x7f);
        var y = LeBytesToInt(bytes);
        if (y >= P) return null;
        var x = RecoverX(y, sign);
        if (x == BigInteger.MinusOne) return null;
        return (x, y, BigInteger.One, ModP(x * y));
    }

    private static BigInteger LeBytesToInt(byte[] bytes)
    {
        // BigInteger ctor is little-endian, signed. Append a zero byte so the
        // result is always non-negative.
        var le = new byte[bytes.Length + 1];
        Buffer.BlockCopy(bytes, 0, le, 0, bytes.Length);
        return new BigInteger(le);
    }
}
