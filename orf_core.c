/*
 * orf_core.c — C accelerator for _find_longest_orf.
 *
 * v2 optimizations over v1:
 *
 *   A. SIMD uppercase (Optimization A)
 *      Instead of a byte-at-a-time UPPER[] table lookup, uppercase_simd()
 *      uses AND-0xDF to clear bit 5 of every byte, converting lowercase
 *      ASCII letters (a-z: 0x61–0x7A) to uppercase (A-Z: 0x41–0x5A) in
 *      wide vector registers.  Safe for DNA alphabets (ACGTN + lowercase).
 *        • NEON:  4 × vld1q_u8 / vandq_u8 / vst1q_u8  = 64 bytes/iteration
 *        • SSE2:  _mm_loadu_si128 / _mm_and_si128 / _mm_store_si128
 *                 = 16 bytes/iteration
 *        • Scalar tail via the existing UPPER[] table for remaining bytes.
 *      Local buffers carry __attribute__((aligned(32))) so SSE2/NEON aligned
 *      stores are safe without runtime alignment checks.
 *
 *   B. Work-stealing thread pool (Optimization B)
 *      Each worker atomically increments a shared counter (WorkQueue.next)
 *      to claim the next unprocessed sequence instead of working a fixed
 *      pre-assigned chunk.  This eliminates idle time when sequences vary
 *      in length.  Each thread owns a reusable 32-byte-aligned buffer
 *      (initial capacity 65536 bytes, grown 1.5× with posix_memalign when
 *      a sequence is larger) to avoid per-sequence malloc/free in the hot
 *      path and to keep SIMD stores aligned.
 *
 * Scans DNA sequences for the longest methionine-initiated ORF across
 * all 6 reading frames (3 forward + 3 reverse complement).
 *
 * The reverse-complement scan reads the forward buffer backwards,
 * checking for complement-reversed codons (CAT→ATG, TTA→TAA, etc.),
 * avoiding a separate RC buffer and halving memory traffic.
 *
 * scan_batch() processes many sequences in parallel using pthreads,
 * with the GIL released during the compute phase.
 *
 * Build:  python3 devtools/build_orf_core.py
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdlib.h>
#include <string.h>
#include <pthread.h>
#include <stdatomic.h>

/* ── SIMD detection and headers ───────────────────────────────────── */

#if defined(__ARM_NEON) || defined(__ARM_NEON__)
#  include <arm_neon.h>
#  define HAS_NEON 1
#  define HAS_SSE2 0
#elif defined(__SSE2__)
#  include <emmintrin.h>
#  define HAS_NEON 0
#  define HAS_SSE2 1
#else
#  define HAS_NEON 0
#  define HAS_SSE2 0
#endif

/* ── Lookup tables ────────────────────────────────────────────────── */

static unsigned char UPPER[256];

static void
init_tables(void)
{
    for (int i = 0; i < 256; i++)
        UPPER[i] = (unsigned char)i;
    UPPER['a'] = 'A'; UPPER['c'] = 'C'; UPPER['g'] = 'G';
    UPPER['t'] = 'T'; UPPER['n'] = 'N';
}

/* ── Complement lookup ────────────────────────────────────────────── */
/*
 * COMPLEMENT maps each nucleotide byte to its Watson-Crick complement.
 * The buffer passed to do_scan_translate() is already uppercased, so only
 * the uppercase entries are exercised in the hot path; lowercase is
 * included for safety.  All other bytes map to themselves.
 */
static unsigned char COMPLEMENT[256];

static void
init_complement(void)
{
    for (int i = 0; i < 256; i++)
        COMPLEMENT[i] = (unsigned char)i;
    COMPLEMENT['A'] = 'T'; COMPLEMENT['T'] = 'A';
    COMPLEMENT['C'] = 'G'; COMPLEMENT['G'] = 'C'; COMPLEMENT['N'] = 'N';
    COMPLEMENT['a'] = 'T'; COMPLEMENT['t'] = 'A';
    COMPLEMENT['c'] = 'G'; COMPLEMENT['g'] = 'C'; COMPLEMENT['n'] = 'N';
}

/* ── Amino-acid codon table ───────────────────────────────────────── */
/*
 * 64-entry table indexed by  i0*16 + i1*4 + i2,
 * where i0..i2 = BASE_IDX[c0..c2]  and  T=0, C=1, A=2, G=3.
 *
 * Layout (groups of 4 share the first two bases; third base varies T/C/A/G):
 *   [0-3]   TT_: TTT TTC TTA TTG  → F F L L
 *   [4-7]   TC_: TCT TCC TCA TCG  → S S S S
 *   [8-11]  TA_: TAT TAC TAA TAG  → Y Y * *
 *   [12-15] TG_: TGT TGC TGA TGG  → C C * W
 *   [16-19] CT_: CTT CTC CTA CTG  → L L L L
 *   [20-23] CC_: CCT CCC CCA CCG  → P P P P
 *   [24-27] CA_: CAT CAC CAA CAG  → H H Q Q
 *   [28-31] CG_: CGT CGC CGA CGG  → R R R R
 *   [32-35] AT_: ATT ATC ATA ATG  → I I I M
 *   [36-39] AC_: ACT ACC ACA ACG  → T T T T
 *   [40-43] AA_: AAT AAC AAA AAG  → N N K K
 *   [44-47] AG_: AGT AGC AGA AGG  → S S R R
 *   [48-51] GT_: GTT GTC GTA GTG  → V V V V
 *   [52-55] GC_: GCT GCC GCA GCG  → A A A A
 *   [56-59] GA_: GAT GAC GAA GAG  → D D E E
 *   [60-63] GG_: GGT GGC GGA GGG  → G G G G
 */
static const char AA_TABLE[64] = {
    /* TT_ */ 'F','F','L','L',  /* TTT TTC TTA TTG */
    /* TC_ */ 'S','S','S','S',  /* TCT TCC TCA TCG */
    /* TA_ */ 'Y','Y','*','*',  /* TAT TAC TAA TAG */
    /* TG_ */ 'C','C','*','W',  /* TGT TGC TGA TGG */
    /* CT_ */ 'L','L','L','L',  /* CTT CTC CTA CTG */
    /* CC_ */ 'P','P','P','P',  /* CCT CCC CCA CCG */
    /* CA_ */ 'H','H','Q','Q',  /* CAT CAC CAA CAG */
    /* CG_ */ 'R','R','R','R',  /* CGT CGC CGA CGG */
    /* AT_ */ 'I','I','I','M',  /* ATT ATC ATA ATG */
    /* AC_ */ 'T','T','T','T',  /* ACT ACC ACA ACG */
    /* AA_ */ 'N','N','K','K',  /* AAT AAC AAA AAG */
    /* AG_ */ 'S','S','R','R',  /* AGT AGC AGA AGG */
    /* GT_ */ 'V','V','V','V',  /* GTT GTC GTA GTG */
    /* GC_ */ 'A','A','A','A',  /* GCT GCC GCA GCG */
    /* GA_ */ 'D','D','E','E',  /* GAT GAC GAA GAG */
    /* GG_ */ 'G','G','G','G',  /* GGT GGC GGA GGG */
};

/*
 * BASE_IDX maps each nucleotide byte to its 2-bit index: T=0, C=1, A=2, G=3.
 * All other values map to -1, causing aa_from_codon() to return 'X'.
 */
static int BASE_IDX[256];

static void
init_base_idx(void)
{
    for (int i = 0; i < 256; i++)
        BASE_IDX[i] = -1;
    BASE_IDX['T'] = 0; BASE_IDX['t'] = 0;
    BASE_IDX['C'] = 1; BASE_IDX['c'] = 1;
    BASE_IDX['A'] = 2; BASE_IDX['a'] = 2;
    BASE_IDX['G'] = 3; BASE_IDX['g'] = 3;
}

static inline char
aa_from_codon(unsigned char c0, unsigned char c1, unsigned char c2)
{
    int i0 = BASE_IDX[c0], i1 = BASE_IDX[c1], i2 = BASE_IDX[c2];
    if (i0 < 0 || i1 < 0 || i2 < 0) return 'X';
    return AA_TABLE[i0 * 16 + i1 * 4 + i2];
}

/* ── SIMD uppercase ───────────────────────────────────────────────────
 *
 * AND-0xDF clears bit 5 of each byte, converting any lowercase ASCII
 * letter to its uppercase counterpart.  This is safe for DNA alphabets
 * (A C G T N + their lowercase equivalents).
 *
 * dst must be at least 32-byte aligned for aligned SSE2 stores;
 * all call sites guarantee this via __attribute__((aligned(32))) or
 * posix_memalign(..., 32, ...).
 */
static void
uppercase_simd(const unsigned char *restrict src,
               unsigned char       *restrict dst,
               Py_ssize_t           len)
{
#if HAS_NEON
    Py_ssize_t i = 0;
    const uint8x16_t mask = vdupq_n_u8(0xDF);
    /* Process 64 bytes per iteration using 4 × 16-byte vector ops. */
    for (; i + 64 <= len; i += 64) {
        uint8x16_t v0 = vld1q_u8(src + i);
        uint8x16_t v1 = vld1q_u8(src + i + 16);
        uint8x16_t v2 = vld1q_u8(src + i + 32);
        uint8x16_t v3 = vld1q_u8(src + i + 48);
        vst1q_u8(dst + i,      vandq_u8(v0, mask));
        vst1q_u8(dst + i + 16, vandq_u8(v1, mask));
        vst1q_u8(dst + i + 32, vandq_u8(v2, mask));
        vst1q_u8(dst + i + 48, vandq_u8(v3, mask));
    }
    /* Scalar tail via UPPER table. */
    for (; i < len; i++)
        dst[i] = UPPER[src[i]];
#elif HAS_SSE2
    Py_ssize_t i = 0;
    const __m128i mask = _mm_set1_epi8((char)0xDF);
    /* Process 16 bytes per iteration; use aligned store (dst is 32-byte aligned). */
    for (; i + 16 <= len; i += 16) {
        __m128i v = _mm_loadu_si128((const __m128i *)(src + i));
        _mm_store_si128((__m128i *)(dst + i), _mm_and_si128(v, mask));
    }
    /* Scalar tail via UPPER table. */
    for (; i < len; i++)
        dst[i] = UPPER[src[i]];
#else
    /* Generic scalar fallback. */
    for (Py_ssize_t i = 0; i < len; i++)
        dst[i] = UPPER[src[i]];
#endif
}

/* ── Codon checks ─────────────────────────────────────────────────── */

#define IS_ATG(c0,c1,c2)  ((c0)=='A' && (c1)=='T' && (c2)=='G')
#define IS_STOP(c0,c1,c2) ((c0)=='T' && \
    (((c1)=='A' && ((c2)=='A' || (c2)=='G')) || ((c1)=='G' && (c2)=='A')))

#define IS_RC_ATG(c0,c1,c2)  ((c0)=='C' && (c1)=='A' && (c2)=='T')
#define IS_RC_STOP(c0,c1,c2) ((c2)=='A' && \
    (((c0)=='T' && ((c1)=='T' || (c1)=='C')) || ((c0)=='C' && (c1)=='T')))

/* ── Core scanner (pure C, no Python API) ─────────────────────────── */

typedef struct {
    int        frame;
    Py_ssize_t atg;
    Py_ssize_t stop;
    Py_ssize_t length;
    int        strand;    /* 0 = fwd, 1 = rc */
} ORFResult;

static void
scan_both(const unsigned char *buf, Py_ssize_t len, ORFResult *best)
{
    best->frame  = -1;
    best->atg    = -1;
    best->stop   = -1;
    best->length = 0;
    best->strand = 0;

    Py_ssize_t end = len - 2;

    /* Forward strand */
    for (int fi = 0; fi < 3; fi++) {
        Py_ssize_t first_atg = -1;
        for (Py_ssize_t pos = fi; pos < end; pos += 3) {
            unsigned char c0 = buf[pos];
            unsigned char c1 = buf[pos + 1];
            unsigned char c2 = buf[pos + 2];
            if (IS_ATG(c0, c1, c2)) {
                if (first_atg == -1) first_atg = pos;
            } else if (IS_STOP(c0, c1, c2)) {
                if (first_atg != -1) {
                    Py_ssize_t l = (pos - first_atg) / 3;
                    if (l > best->length) {
                        best->length = l;
                        best->frame  = fi;
                        best->atg    = first_atg;
                        best->stop   = pos;
                        best->strand = 0;
                    }
                }
                first_atg = -1;
            }
        }
    }

    /* Reverse complement via reverse read */
    for (int fi = 0; fi < 3; fi++) {
        Py_ssize_t first_atg_j = -1;
        for (Py_ssize_t p = len - 3 - fi; p >= 0; p -= 3) {
            unsigned char c0 = buf[p];
            unsigned char c1 = buf[p + 1];
            unsigned char c2 = buf[p + 2];
            Py_ssize_t rc_j = len - 3 - p;
            if (IS_RC_ATG(c0, c1, c2)) {
                if (first_atg_j == -1) first_atg_j = rc_j;
            } else if (IS_RC_STOP(c0, c1, c2)) {
                if (first_atg_j != -1) {
                    Py_ssize_t l = (rc_j - first_atg_j) / 3;
                    if (l > best->length) {
                        best->length = l;
                        best->frame  = fi;
                        best->atg    = first_atg_j;
                        best->stop   = rc_j;
                        best->strand = 1;
                    }
                }
                first_atg_j = -1;
            }
        }
    }
}

/* ── Per-thread reusable aligned buffer ───────────────────────────── */

/*
 * Each worker thread owns one ThreadBuf.  It starts at 64 KiB and grows
 * 1.5× (via posix_memalign) whenever a sequence exceeds the current
 * capacity.  The buffer is always 32-byte aligned so SIMD stores are safe.
 */
typedef struct {
    unsigned char *buf;
    Py_ssize_t     cap;
} ThreadBuf;

static void
thread_buf_init(ThreadBuf *tb)
{
    tb->cap = 65536;
    if (posix_memalign((void **)&tb->buf, 32, (size_t)tb->cap) != 0) {
        tb->buf = NULL;
        tb->cap = 0;
    }
}

/*
 * Ensure tb->buf can hold at least `need` bytes.
 * Grows capacity by 1.5× until large enough.
 * Returns 1 on success, 0 on allocation failure.
 */
static int
thread_buf_ensure(ThreadBuf *tb, Py_ssize_t need)
{
    if (tb->buf != NULL && need <= tb->cap)
        return 1;

    Py_ssize_t newcap = (tb->cap > 0) ? tb->cap : 65536;
    while (newcap < need)
        newcap = newcap + newcap / 2;   /* 1.5× growth */

    free(tb->buf);
    tb->buf = NULL;
    if (posix_memalign((void **)&tb->buf, 32, (size_t)newcap) != 0) {
        tb->cap = 0;
        return 0;
    }
    tb->cap = newcap;
    return 1;
}

static void
thread_buf_free(ThreadBuf *tb)
{
    free(tb->buf);
    tb->buf = NULL;
    tb->cap = 0;
}

/* ── scan helpers ─────────────────────────────────────────────────── */

/*
 * Internal helper: uppercase src into a caller-supplied buffer, then scan.
 * buf must be at least len bytes and 32-byte aligned.
 * Pure C — no Python API, safe to call without the GIL.
 */
static void
scan_one_with_buf(const unsigned char *raw, Py_ssize_t len,
                  ORFResult *result, unsigned char *buf)
{
    uppercase_simd(raw, buf, len);
    scan_both(buf, len, result);
}

/*
 * Convenience wrapper: manages its own buffer.
 * Uses a 32-byte-aligned stack buffer for short sequences;
 * falls back to posix_memalign for longer ones.
 * Pure C — no Python API, safe to call without the GIL.
 */
static void
scan_one(const unsigned char *raw, Py_ssize_t len, ORFResult *result)
{
    unsigned char stack_buf[16384] __attribute__((aligned(32)));
    unsigned char *buf;
    int heap = (len > (Py_ssize_t)sizeof(stack_buf));

    if (heap) {
        if (posix_memalign((void **)&buf, 32, (size_t)len) != 0) {
            result->length = 0;
            result->frame  = -1;
            return;
        }
    } else {
        buf = stack_buf;
    }

    scan_one_with_buf(raw, len, result, buf);

    if (heap) free(buf);
}

/* ── Batch scanner with work-stealing pthreads ────────────────────── */

typedef struct {
    const unsigned char *raw;
    Py_ssize_t           len;
    ORFResult            result;
} ScanTask;

/*
 * Work queue shared among all worker threads.
 * `next` is an atomic counter: each thread claims one task at a time by
 * incrementing it, so threads naturally steal work without any idle time
 * when sequence lengths are uneven.
 */
typedef struct {
    ScanTask           *tasks;
    Py_ssize_t          n;
    _Atomic Py_ssize_t  next;   /* work-stealing counter */
} WorkQueue;

static void *
worker_thread(void *arg)
{
    WorkQueue *q = (WorkQueue *)arg;

    /* Each thread owns a reusable aligned buffer — no per-sequence alloc. */
    ThreadBuf tb;
    thread_buf_init(&tb);

    for (;;) {
        Py_ssize_t i = atomic_fetch_add(&q->next, (Py_ssize_t)1);
        if (i >= q->n) break;

        ScanTask *t = &q->tasks[i];
        if (thread_buf_ensure(&tb, t->len)) {
            scan_one_with_buf(t->raw, t->len, &t->result, tb.buf);
        } else {
            /* Allocation failed: fall back to per-call alloc. */
            scan_one(t->raw, t->len, &t->result);
        }
    }

    thread_buf_free(&tb);
    return NULL;
}

/* ── Python-facing functions ──────────────────────────────────────── */

/*
 * scan_both_strands(nucleotide: str|bytes)
 *   -> (strand_idx, frame, atg_pos, stop_pos, aa_length)
 */
static PyObject *
py_scan_both_strands(PyObject *self, PyObject *args)
{
    PyObject *obj;
    const unsigned char *raw;
    Py_ssize_t len;

    if (!PyArg_ParseTuple(args, "O", &obj))
        return NULL;

    if (PyUnicode_Check(obj)) {
        const char *utf8 = PyUnicode_AsUTF8AndSize(obj, &len);
        if (!utf8) return NULL;
        raw = (const unsigned char *)utf8;
    } else if (PyBytes_Check(obj)) {
        raw = (const unsigned char *)PyBytes_AS_STRING(obj);
        len = PyBytes_GET_SIZE(obj);
    } else {
        PyErr_SetString(PyExc_TypeError, "expected str or bytes");
        return NULL;
    }

    ORFResult best;
    scan_one(raw, len, &best);

    return Py_BuildValue("(innnn)",
                         best.strand,
                         best.frame, best.atg, best.stop, best.length);
}

/*
 * scan_batch(sequences: list[str], nthreads: int)
 *   -> list[(strand_idx, frame, atg_pos, stop_pos, aa_length)]
 *
 * Scans all sequences in parallel using a work-stealing pthread pool.
 * The GIL is released during the compute phase.
 * Falls back to single-threaded (with a reusable aligned buffer) when
 * nthreads <= 1.
 */
static PyObject *
py_scan_batch(PyObject *self, PyObject *args)
{
    PyObject *seq_list;
    int nthreads;
    if (!PyArg_ParseTuple(args, "Oi", &seq_list, &nthreads))
        return NULL;

    if (!PyList_Check(seq_list)) {
        PyErr_SetString(PyExc_TypeError, "first argument must be a list");
        return NULL;
    }

    Py_ssize_t n = PyList_GET_SIZE(seq_list);
    if (n == 0)
        return PyList_New(0);

    /* Phase 1 (GIL held): extract raw pointers from Python objects. */
    ScanTask *tasks = (ScanTask *)calloc(n, sizeof(ScanTask));
    if (!tasks) return PyErr_NoMemory();

    for (Py_ssize_t i = 0; i < n; i++) {
        PyObject *obj = PyList_GET_ITEM(seq_list, i);
        if (PyUnicode_Check(obj)) {
            const char *utf8 = PyUnicode_AsUTF8AndSize(obj, &tasks[i].len);
            if (!utf8) { free(tasks); return NULL; }
            tasks[i].raw = (const unsigned char *)utf8;
        } else if (PyBytes_Check(obj)) {
            tasks[i].raw = (const unsigned char *)PyBytes_AS_STRING(obj);
            tasks[i].len = PyBytes_GET_SIZE(obj);
        } else {
            free(tasks);
            PyErr_SetString(PyExc_TypeError,
                            "list elements must be str or bytes");
            return NULL;
        }
    }

    /* Phase 2 (GIL released): scan in parallel. */
    if (nthreads < 1) nthreads = 1;
    if (nthreads > (int)n) nthreads = (int)n;

    Py_BEGIN_ALLOW_THREADS

    if (nthreads == 1) {
        /*
         * Single-threaded fast path: use a single reusable aligned buffer
         * (SIMD uppercase + no per-sequence malloc).
         */
        ThreadBuf tb;
        thread_buf_init(&tb);
        for (Py_ssize_t i = 0; i < n; i++) {
            if (thread_buf_ensure(&tb, tasks[i].len)) {
                scan_one_with_buf(tasks[i].raw, tasks[i].len,
                                  &tasks[i].result, tb.buf);
            } else {
                scan_one(tasks[i].raw, tasks[i].len, &tasks[i].result);
            }
        }
        thread_buf_free(&tb);
    } else {
        /* Multi-threaded path: work-stealing pool. */
        WorkQueue q;
        q.tasks = tasks;
        q.n     = n;
        atomic_init(&q.next, (Py_ssize_t)0);

        pthread_t *threads = (pthread_t *)malloc(nthreads * sizeof(pthread_t));
        for (int t = 0; t < nthreads; t++)
            pthread_create(&threads[t], NULL, worker_thread, &q);
        for (int t = 0; t < nthreads; t++)
            pthread_join(threads[t], NULL);
        free(threads);
    }

    Py_END_ALLOW_THREADS

    /* Phase 3 (GIL held): build result list. */
    PyObject *result_list = PyList_New(n);
    if (!result_list) { free(tasks); return NULL; }

    for (Py_ssize_t i = 0; i < n; i++) {
        ORFResult *r = &tasks[i].result;
        PyObject *tup = Py_BuildValue("(innnn)",
                                      r->strand, r->frame,
                                      r->atg, r->stop, r->length);
        if (!tup) {
            Py_DECREF(result_list);
            free(tasks);
            return NULL;
        }
        PyList_SET_ITEM(result_list, i, tup);
    }

    free(tasks);
    return result_list;
}

/* ── Batch scanner with in-C translation ─────────────────────────── */

/*
 * Extended per-task descriptor — same layout as ScanTask but with extra
 * fields for the translated amino-acid sequence and stop codon count.
 * calloc() guarantees aa_seq == NULL and stop_count == 0 on allocation.
 */
typedef struct {
    const unsigned char *raw;
    Py_ssize_t           len;
    ORFResult            result;
    char                *aa_seq;    /* malloc'd AA bytes; NULL = no ORF */
    Py_ssize_t           aa_len;
    int                  stop_count;
} ScanTaskFull;

/*
 * Work queue for the translate worker pool.
 * Mirrors WorkQueue but references ScanTaskFull instead of ScanTask.
 */
typedef struct {
    ScanTaskFull       *tasks;
    Py_ssize_t          n;
    _Atomic Py_ssize_t  next;
} WorkQueueFull;

/*
 * do_scan_translate — given a ScanTaskFull with result already filled and
 * an uppercased buffer of length len, allocates t->aa_seq and translates
 * the winning ORF, then sets t->stop_count.
 *
 * If result.length < 30 the function returns immediately (aa_seq stays
 * NULL).  On malloc failure aa_seq is left NULL and stop_count is set
 * to 1 so Phase 3 can still build a valid tuple.
 *
 * Pure C — no Python API, safe to call without the GIL.
 */
static void
do_scan_translate(ScanTaskFull *t, const unsigned char *buf, Py_ssize_t len)
{
    if (t->result.length < 30)
        return;   /* aa_seq stays NULL */

    Py_ssize_t aa_len = t->result.length;
    char *aa = (char *)malloc((size_t)(aa_len + 1));
    if (aa == NULL) {
        t->aa_seq     = NULL;
        t->stop_count = 1;
        return;
    }

    if (t->result.strand == 0) {
        /* ── Forward strand ──────────────────────────────────── */
        for (Py_ssize_t j = 0; j < aa_len; j++) {
            Py_ssize_t p = t->result.atg + j * 3;
            aa[j] = aa_from_codon(buf[p], buf[p + 1], buf[p + 2]);
        }
        /* Count consecutive in-frame stop codons after the first stop. */
        int ns = 1;
        Py_ssize_t probe = t->result.stop + 3;
        while (probe + 3 <= len &&
               IS_STOP(buf[probe], buf[probe + 1], buf[probe + 2])) {
            ns++;
            probe += 3;
        }
        t->stop_count = ns;
    } else {
        /* ── Reverse-complement strand ───────────────────────── */
        /*
         * The scanner identifies ATG/stop by reading the *forward* buffer
         * in reverse.  result.atg and result.stop are in RC-strand coords:
         *   rc_pos maps back to forward byte at (len - 1 - rc_pos).
         *
         * strand_b[rc_pos]   = COMPLEMENT( buf[len-1-rc_pos]   )
         * strand_b[rc_pos+1] = COMPLEMENT( buf[len-2-rc_pos]   )
         * strand_b[rc_pos+2] = COMPLEMENT( buf[len-3-rc_pos]   )
         */
        for (Py_ssize_t j = 0; j < aa_len; j++) {
            Py_ssize_t rc_pos   = t->result.atg + j * 3;
            Py_ssize_t fwd_base = len - 1 - rc_pos;
            unsigned char c0 = COMPLEMENT[buf[fwd_base]];
            unsigned char c1 = COMPLEMENT[buf[fwd_base - 1]];
            unsigned char c2 = COMPLEMENT[buf[fwd_base - 2]];
            aa[j] = aa_from_codon(c0, c1, c2);
        }
        /* Count consecutive in-frame stop codons on the RC strand.
         * RC probe position rc_probe maps to forward index (len-3-rc_probe);
         * IS_RC_STOP checks for TAA/TGA/TAG as seen in the forward buffer. */
        int ns = 1;
        Py_ssize_t rc_probe = t->result.stop + 3;
        for (;;) {
            Py_ssize_t fwd_p = len - 3 - rc_probe;
            if (fwd_p < 0) break;
            if (!IS_RC_STOP(buf[fwd_p], buf[fwd_p + 1], buf[fwd_p + 2])) break;
            ns++;
            rc_probe += 3;
        }
        t->stop_count = ns;
    }

    aa[aa_len] = '\0';
    t->aa_seq  = aa;
    t->aa_len  = aa_len;
}

static void *
worker_thread_translate(void *arg)
{
    WorkQueueFull *q = (WorkQueueFull *)arg;

    /* Each thread owns a reusable aligned buffer — no per-sequence alloc. */
    ThreadBuf tb;
    thread_buf_init(&tb);

    for (;;) {
        Py_ssize_t i = atomic_fetch_add(&q->next, (Py_ssize_t)1);
        if (i >= q->n) break;

        ScanTaskFull *t  = &q->tasks[i];
        Py_ssize_t   len = t->len;

        if (!thread_buf_ensure(&tb, len)) {
            /* Allocation failed: scan without translation; aa_seq stays NULL. */
            scan_one(t->raw, len, &t->result);
        } else {
            unsigned char *buf = tb.buf;
            uppercase_simd(t->raw, buf, len);
            scan_both(buf, len, &t->result);
            do_scan_translate(t, buf, len);
        }
    }

    thread_buf_free(&tb);
    return NULL;
}

/*
 * scan_batch_translate(sequences: list[str], nthreads: int)
 *   -> list[(strand_idx, frame, atg_pos, stop_pos, aa_length, aa_seq, stop_count)]
 *
 * Like scan_batch but also performs in-C translation, returning the amino-
 * acid sequence as a bytes object so Python does no O(seq_len) work per
 * result.  The GIL is released during the compute phase.
 */
static PyObject *
py_scan_batch_translate(PyObject *self, PyObject *args)
{
    PyObject *seq_list;
    int nthreads;
    if (!PyArg_ParseTuple(args, "Oi", &seq_list, &nthreads))
        return NULL;

    if (!PyList_Check(seq_list)) {
        PyErr_SetString(PyExc_TypeError, "first argument must be a list");
        return NULL;
    }

    Py_ssize_t n = PyList_GET_SIZE(seq_list);
    if (n == 0)
        return PyList_New(0);

    /* Phase 1 (GIL held): extract raw pointers.
     * calloc zeroes aa_seq, aa_len, and stop_count for every task. */
    ScanTaskFull *tasks = (ScanTaskFull *)calloc(n, sizeof(ScanTaskFull));
    if (!tasks) return PyErr_NoMemory();

    for (Py_ssize_t i = 0; i < n; i++) {
        PyObject *obj = PyList_GET_ITEM(seq_list, i);
        if (PyUnicode_Check(obj)) {
            const char *utf8 = PyUnicode_AsUTF8AndSize(obj, &tasks[i].len);
            if (!utf8) { free(tasks); return NULL; }
            tasks[i].raw = (const unsigned char *)utf8;
        } else if (PyBytes_Check(obj)) {
            tasks[i].raw = (const unsigned char *)PyBytes_AS_STRING(obj);
            tasks[i].len = PyBytes_GET_SIZE(obj);
        } else {
            free(tasks);
            PyErr_SetString(PyExc_TypeError,
                            "list elements must be str or bytes");
            return NULL;
        }
    }

    /* Phase 2 (GIL released): scan and translate in parallel. */
    if (nthreads < 1) nthreads = 1;
    if (nthreads > (int)n) nthreads = (int)n;

    Py_BEGIN_ALLOW_THREADS

    if (nthreads == 1) {
        /*
         * Single-threaded fast path: one reusable aligned buffer,
         * same uppercase/scan/translate logic as worker_thread_translate.
         */
        ThreadBuf tb;
        thread_buf_init(&tb);
        for (Py_ssize_t i = 0; i < n; i++) {
            ScanTaskFull *t  = &tasks[i];
            Py_ssize_t   len = t->len;
            if (!thread_buf_ensure(&tb, len)) {
                scan_one(t->raw, len, &t->result);
            } else {
                unsigned char *buf = tb.buf;
                uppercase_simd(t->raw, buf, len);
                scan_both(buf, len, &t->result);
                do_scan_translate(t, buf, len);
            }
        }
        thread_buf_free(&tb);
    } else {
        /* Multi-threaded path: work-stealing pool. */
        WorkQueueFull q;
        q.tasks = tasks;
        q.n     = n;
        atomic_init(&q.next, (Py_ssize_t)0);

        pthread_t *threads = (pthread_t *)malloc(nthreads * sizeof(pthread_t));
        for (int t = 0; t < nthreads; t++)
            pthread_create(&threads[t], NULL, worker_thread_translate, &q);
        for (int t = 0; t < nthreads; t++)
            pthread_join(threads[t], NULL);
        free(threads);
    }

    Py_END_ALLOW_THREADS

    /* Phase 3 (GIL held): build result list.
     * Each tasks[i].aa_seq is freed here whether NULL or not (guarded). */
    PyObject *result_list = PyList_New(n);
    if (!result_list) {
        for (Py_ssize_t i = 0; i < n; i++) {
            if (tasks[i].aa_seq) free(tasks[i].aa_seq);
        }
        free(tasks);
        return NULL;
    }

    for (Py_ssize_t i = 0; i < n; i++) {
        ORFResult *r = &tasks[i].result;

        /* Extract aa_seq before any Python allocation; NULL the pointer so
         * the error-path cleanup loop below won't double-free it. */
        char      *raw_aa     = tasks[i].aa_seq;
        Py_ssize_t raw_aa_len = tasks[i].aa_len;
        int        stop_count = tasks[i].stop_count;
        tasks[i].aa_seq = NULL;

        PyObject *aa_bytes;
        if (raw_aa != NULL) {
            aa_bytes = PyBytes_FromStringAndSize(raw_aa, raw_aa_len);
            free(raw_aa);
        } else {
            aa_bytes = PyBytes_FromStringAndSize("", 0);
            stop_count = 0;
        }
        if (!aa_bytes) {
            for (Py_ssize_t j = i + 1; j < n; j++) {
                if (tasks[j].aa_seq) free(tasks[j].aa_seq);
            }
            Py_DECREF(result_list);
            free(tasks);
            return NULL;
        }

        PyObject *tup = Py_BuildValue("(innnnOi)",
                                      r->strand, r->frame,
                                      r->atg, r->stop, r->length,
                                      aa_bytes, stop_count);
        Py_DECREF(aa_bytes);

        if (!tup) {
            for (Py_ssize_t j = i + 1; j < n; j++) {
                if (tasks[j].aa_seq) free(tasks[j].aa_seq);
            }
            Py_DECREF(result_list);
            free(tasks);
            return NULL;
        }
        PyList_SET_ITEM(result_list, i, tup);
    }

    free(tasks);
    return result_list;
}

/*
 * scan_strand(strand: bytes) -> (frame, atg_pos, stop_pos, aa_length)
 */
static PyObject *
py_scan_strand(PyObject *self, PyObject *args)
{
    Py_buffer pybuf;
    if (!PyArg_ParseTuple(args, "y*", &pybuf))
        return NULL;

    ORFResult best;
    best.frame = -1; best.atg = -1; best.stop = -1;
    best.length = 0; best.strand = 0;

    Py_ssize_t end = pybuf.len - 2;
    const unsigned char *raw = (const unsigned char *)pybuf.buf;
    for (int fi = 0; fi < 3; fi++) {
        Py_ssize_t first_atg = -1;
        for (Py_ssize_t pos = fi; pos < end; pos += 3) {
            unsigned char c0 = raw[pos], c1 = raw[pos+1], c2 = raw[pos+2];
            if (IS_ATG(c0,c1,c2)) {
                if (first_atg == -1) first_atg = pos;
            } else if (IS_STOP(c0,c1,c2)) {
                if (first_atg != -1) {
                    Py_ssize_t l = (pos - first_atg) / 3;
                    if (l > best.length) {
                        best.length = l; best.frame = fi;
                        best.atg = first_atg; best.stop = pos;
                    }
                }
                first_atg = -1;
            }
        }
    }

    PyBuffer_Release(&pybuf);
    return Py_BuildValue("(nnnn)",
                         (Py_ssize_t)best.frame, best.atg,
                         best.stop, best.length);
}

/* ── Module definition ────────────────────────────────────────────── */

static PyMethodDef orf_core_methods[] = {
    {"scan_strand", py_scan_strand, METH_VARARGS,
     "scan_strand(strand: bytes) -> (frame, atg_pos, stop_pos, aa_length)\n"
     "Scan one uppercase strand for the longest M-initiated ORF."},
    {"scan_both_strands", py_scan_both_strands, METH_VARARGS,
     "scan_both_strands(nucleotide: str|bytes) -> (strand, frame, atg, stop, length)\n"
     "Uppercase, compute RC, scan both strands. strand: 0=fwd, 1=rc."},
    {"scan_batch", py_scan_batch, METH_VARARGS,
     "scan_batch(sequences: list[str], nthreads: int) -> list[tuple]\n"
     "Scan all sequences in parallel using a work-stealing pthread pool.\n"
     "Returns list of (strand, frame, atg, stop, length) tuples."},
    {"scan_batch_translate", py_scan_batch_translate, METH_VARARGS,
     "scan_batch_translate(sequences: list[str], nthreads: int) -> list[tuple]\n"
     "Like scan_batch but also returns (aa_seq: bytes, stop_count: int) per sequence.\n"
     "Returns list of (strand, frame, atg, stop, length, aa_seq, stop_count)."},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef orf_core_module = {
    PyModuleDef_HEAD_INIT,
    "orf_core",
    "C accelerator for ORF scanning.",
    -1,
    orf_core_methods
};

PyMODINIT_FUNC
PyInit_orf_core(void)
{
    init_tables();
    init_complement();
    init_base_idx();
    return PyModule_Create(&orf_core_module);
}