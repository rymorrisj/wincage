#pragma once
// Minimal JSON parser scoped to LaunchConfig. No error recovery.
// Throws std::runtime_error on malformed input or missing fields.
#include <cctype>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <map>
#include <stdexcept>
#include <string>
#include <vector>

struct JVal {
    enum class T { Obj, Arr, Str, Num, Bool, Null } tag = T::Null;
    std::map<std::string, JVal> obj;
    std::vector<JVal>           arr;
    std::string                 str;
    double                      num = 0;
    bool                        b   = false;

    const JVal& at(const std::string& k) const {
        auto it = obj.find(k);
        if (it == obj.end()) throw std::runtime_error("missing field: " + k);
        return it->second;
    }
    std::string value(const std::string& k, const std::string& def) const {
        auto it = obj.find(k);
        return (it != obj.end() && it->second.tag == T::Str) ? it->second.str : def;
    }
    bool value(const std::string& k, bool def) const {
        auto it = obj.find(k);
        return (it != obj.end() && it->second.tag == T::Bool) ? it->second.b : def;
    }
    template<class V> V get() const;
};

template<> inline std::string        JVal::get<std::string>()           const {
    if (tag != T::Str)  throw std::runtime_error("expected string");
    return str;
}
// Casting an out of range or negative double to an unsigned integer type is
// undefined behaviour in C++ (not just a truncating wraparound), so every
// numeric field pulled from the launch JSON (cpu_max_rate, parent_pid,
// memory_limit_mb, ...) is range-checked here before the cast, rather than
// trusting the Python side to have only ever sent well-formed values.
template<> inline unsigned long      JVal::get<unsigned long>()         const {
    if (tag != T::Num)  throw std::runtime_error("expected number");
    // NaN compares false against every bound, including itself, so it slips
    // through a plain range check; isnan() catches it explicitly.
    if (std::isnan(num) || num < 0 || num > 4294967295.0 /* ULONG_MAX on Win32/Win64 */)
        throw std::runtime_error("number out of range for unsigned long");
    return static_cast<unsigned long>(num);
}
template<> inline unsigned long long JVal::get<unsigned long long>()    const {
    if (tag != T::Num)  throw std::runtime_error("expected number");
    if (std::isnan(num) || num < 0 || num > 18446744073709551615.0 /* ULLONG_MAX, nearest double */)
        throw std::runtime_error("number out of range for unsigned long long");
    return static_cast<unsigned long long>(num);
}
template<> inline bool               JVal::get<bool>()                  const {
    if (tag != T::Bool) throw std::runtime_error("expected bool");
    return b;
}

namespace json_detail {

// Appends the UTF-8 encoding of one Unicode code point to *s*. Used to decode
// \uXXXX escapes (and surrogate pairs) into the UTF-8 byte strings JVal::str
// holds elsewhere in this parser.
inline void append_utf8(std::string& s, unsigned int cp) {
    if (cp <= 0x7F) {
        s += static_cast<char>(cp);
    } else if (cp <= 0x7FF) {
        s += static_cast<char>(0xC0 | (cp >> 6));
        s += static_cast<char>(0x80 | (cp & 0x3F));
    } else if (cp <= 0xFFFF) {
        s += static_cast<char>(0xE0 | (cp >> 12));
        s += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
        s += static_cast<char>(0x80 | (cp & 0x3F));
    } else {
        s += static_cast<char>(0xF0 | (cp >> 18));
        s += static_cast<char>(0x80 | ((cp >> 12) & 0x3F));
        s += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
        s += static_cast<char>(0x80 | (cp & 0x3F));
    }
}

struct Parser {
    const char* p;
    const char* end;
    explicit Parser(const std::string& s) : p(s.data()), end(s.data() + s.size()) {}

    void ws()  { while (p < end && std::isspace((unsigned char)*p)) ++p; }
    char peek(){ ws(); return p < end ? *p : '\0'; }
    void eat(char c) {
        ws();
        if (p >= end || *p != c)
            throw std::runtime_error(std::string("expected '") + c + '\'');
        ++p;
    }
    // p points at 'u'; reads the 4 hex digits that follow it and leaves p on
    // the last of those digits (matching every other case in parse_str()'s
    // switch, where p is left on the last character consumed).
    unsigned int parse_hex4() {
        if (p + 4 >= end) throw std::runtime_error("truncated \\u escape");
        unsigned int v = 0;
        for (int i = 1; i <= 4; ++i) {
            char c = p[i];
            v <<= 4;
            if (c >= '0' && c <= '9')      v |= static_cast<unsigned int>(c - '0');
            else if (c >= 'a' && c <= 'f') v |= static_cast<unsigned int>(c - 'a' + 10);
            else if (c >= 'A' && c <= 'F') v |= static_cast<unsigned int>(c - 'A' + 10);
            else throw std::runtime_error("invalid \\u hex digit");
        }
        p += 4;
        return v;
    }
    std::string parse_str() {
        eat('"');
        std::string s;
        while (p < end && *p != '"') {
            if (*p == '\\') {
                if (++p >= end) throw std::runtime_error("truncated escape");
                switch (*p) {
                    case '"':  s += '"';  break;
                    case '\\': s += '\\'; break;
                    case '/':  s += '/';  break;
                    case 'n':  s += '\n'; break;
                    case 'r':  s += '\r'; break;
                    case 't':  s += '\t'; break;
                    case 'u': {
                        unsigned int cp = parse_hex4();
                        if (cp >= 0xD800 && cp <= 0xDBFF) {
                            // High surrogate: must be followed by a low
                            // surrogate escape to combine into one code point.
                            if (p + 2 >= end || p[1] != '\\' || p[2] != 'u')
                                throw std::runtime_error("unpaired UTF-16 high surrogate");
                            p += 2;
                            unsigned int lo = parse_hex4();
                            if (lo < 0xDC00 || lo > 0xDFFF)
                                throw std::runtime_error("invalid UTF-16 low surrogate");
                            cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                        } else if (cp >= 0xDC00 && cp <= 0xDFFF) {
                            throw std::runtime_error("unpaired UTF-16 low surrogate");
                        }
                        append_utf8(s, cp);
                        break;
                    }
                    default:   s += *p;   break;
                }
            } else {
                s += *p;
            }
            ++p;
        }
        eat('"');
        return s;
    }
    JVal parse_val();
    JVal parse_obj() {
        eat('{');
        JVal v; v.tag = JVal::T::Obj;
        if (peek() == '}') { ++p; return v; }
        for (;;) {
            std::string k = parse_str();
            eat(':');
            v.obj[k] = parse_val();
            if (peek() == '}') { ++p; break; }
            eat(',');
        }
        return v;
    }
    JVal parse_arr() {
        eat('[');
        JVal v; v.tag = JVal::T::Arr;
        if (peek() == ']') { ++p; return v; }
        for (;;) {
            v.arr.push_back(parse_val());
            if (peek() == ']') { ++p; break; }
            eat(',');
        }
        return v;
    }
};

inline JVal Parser::parse_val() {
    char c = peek();
    if (c == '{') return parse_obj();
    if (c == '[') return parse_arr();
    if (c == '"') { JVal v; v.tag = JVal::T::Str; v.str = parse_str(); return v; }
    if (c == 't' || c == 'f') {
        bool bv = (c == 't');
        const char* lit = bv ? "true" : "false";
        size_t      len = bv ? 4      : 5;
        if (std::strncmp(p, lit, len) != 0) throw std::runtime_error("invalid literal");
        p += len;
        JVal v; v.tag = JVal::T::Bool; v.b = bv;
        return v;
    }
    if (c == 'n') {
        if (std::strncmp(p, "null", 4) != 0) throw std::runtime_error("invalid literal");
        p += 4;
        JVal v; v.tag = JVal::T::Null;
        return v;
    }
    char* ep;
    double d = std::strtod(p, &ep);
    if (ep == p) throw std::runtime_error("expected JSON value");
    p = ep;
    JVal v; v.tag = JVal::T::Num; v.num = d;
    return v;
}
} // namespace json_detail

inline JVal json_parse(const std::string& s) {
    json_detail::Parser parser(s);
    JVal v = parser.parse_val();
    parser.ws();
    if (parser.p != parser.end) {
        throw std::runtime_error("unexpected trailing content after JSON value");
    }
    return v;
}

// Flat JSON object builder for output.
class JsonOut {
    std::string buf_;
    bool        first_ = true;
    static std::string quote(const std::string& s) {
        std::string r = "\"";
        for (char c : s) {
            if      (c == '"')  r += "\\\"";
            else if (c == '\\') r += "\\\\";
            else if (c == '\n') r += "\\n";
            else if (c == '\r') r += "\\r";
            else                r += c;
        }
        return r + '"';
    }
    void sep() { if (!first_) buf_ += ','; first_ = false; }
public:
    JsonOut() { buf_ = '{'; }
    JsonOut& set(const std::string& k, const std::string& v)
        { sep(); buf_ += quote(k) + ':' + quote(v); return *this; }
    JsonOut& set(const std::string& k, long long v)
        { sep(); buf_ += quote(k) + ':' + std::to_string(v); return *this; }
    JsonOut& set(const std::string& k, bool v)
        { sep(); buf_ += quote(k) + ':' + (v ? "true" : "false"); return *this; }
    std::string dump() const { return buf_ + '}'; }
};
