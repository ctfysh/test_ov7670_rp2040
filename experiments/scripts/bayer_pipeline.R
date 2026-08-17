#!/usr/bin/env Rscript
# OV7670 Raw Bayer 处理管线 (bayer_pipeline.R)
#
# 用 R 语言完整重现 bayer_pipeline.py 的取证管线:
#   fix_dead_pixels        : 散点死点修复 (同色 4-邻域中值, 差>阈 替换)
#   fix_defect_region      : 楔形区域缺陷修复 (带外同相位参考插值, 逐像素判定)
#   demosaic_bayer         : 双线性 G-first + G 校正去马赛克 (valid-mask, 无 padding)
#   render_stitched        : 上/下半窗分相位去马赛克 + 拼接 + WB + 伽马
#   pipeline               : 完整管线 -> RGB 数组 + 修复统计
#
# 解码模式:
# - COM15=0xC0 (当前): 8-bit 全独立, raw 直通, 不需要 g_map.
# - COM15=0xD0 (旧): 6-bit 有效 (bit0==bit7 镜像, bit2 噪声), 需要 g_map 解码.
#   旧模式向后兼容: pipeline(..., use_gmap=TRUE, map="old"|"new").
#
# 相位: 上下窗均 BGGR (COM15=0xC0 后全帧 CFA pattern 一致)
# 灰度世界 WB, 伽马 0.85 提亮
#
# 用法:
#   Rscript bayer_pipeline.R input.raw -o out.png
#   Rscript bayer_pipeline.R input.raw -o out.png --use-gmap --map old  # 旧模式

suppressPackageStartupMessages(library(png))  # writePNG

# --- g_map 保留供向后兼容 (COM15=0xD0 旧模式) ---
# map="old": g(v)=128*b1+64*b7+32*b6+16*b5+8*b4+4*b3 (08-14 会话)
# map="new": g(v)=128*b2+64*b7+32*b6+16*b5+8*b4+4*b3 (08-16 会话)
# COM15=0xC0 模式下 8 位全部独立有效, 直接用原始字节, 不需要 g_map.
g_map <- function(v, map = "old") {
    dims <- dim(v)
    v <- as.integer(v)
    b7 <- bitwAnd(v %/% 128L, 1L)
    b6 <- bitwAnd(v %/% 64L, 1L)
    b5 <- bitwAnd(v %/% 32L, 1L)
    b4 <- bitwAnd(v %/% 16L, 1L)
    b3 <- bitwAnd(v %/% 8L, 1L)
    msb <- if (map == "new") bitwAnd(v %/% 4L, 1L) else bitwAnd(v %/% 2L, 1L)
    out <- as.integer(128 * msb + 64 * b7 + 32 * b6 + 16 * b5 + 8 * b4 + 4 * b3)
    dim(out) <- dims   # 恢复矩阵形状
    out
}

# --- 矩阵平移辅助: out[y,x] = mat[y+dy, x+dx], 越界处为 0 ---
# (等价于 numpy np.roll + _valid_mask 的界内贡献; 目标掩码同样平移, 越界为 0)
shift_matrix <- function(mat, dy, dx) {
    h <- nrow(mat)
    w <- ncol(mat)
    out <- matrix(0, h, w)
    src_rows <- (1:h) + dy
    src_cols <- (1:w) + dx
    rows_ok <- src_rows >= 1L & src_rows <= h
    cols_ok <- src_cols >= 1L & src_cols <= w
    if (any(rows_ok) && any(cols_ok)) {
        out[rows_ok, cols_ok] <- mat[src_rows[rows_ok], src_cols[cols_ok], drop = FALSE]
    }
    out
}

# --- 邻域平均: 对每个 kernel 偏移, 只累加目标掩码为真且在界内的源值 ---
gather_avg <- function(src, h, w, target_mask, kernel) {
    vals <- matrix(0, h, w)
    cnt <- matrix(0, h, w)
    for (k in kernel) {
        dy <- k[1]
        dx <- k[2]
        shifted_src <- shift_matrix(src, dy, dx)
        shifted_tgt <- shift_matrix(target_mask, dy, dx)
        take <- shifted_tgt > 0
        vals <- vals + ifelse(take, shifted_src, 0)
        cnt <- cnt + take
    }
    vals / pmax(cnt, 1)
}

# --- 颜色映射: (h,w) 字符数组, pattern 行主序覆盖 2x2 块 ---
color_map <- function(pattern, h, w) {
    yy <- (0:(h - 1)) %% 2
    xx <- (0:(w - 1)) %% 2
    idx <- outer(yy * 2, xx, `+`) + 1L
    chars <- strsplit(pattern, "")[[1]]
    matrix(chars[idx], h, w)
}

# --- 双线性去马赛克 (G-first + G 校正 R/B), 返回 0..255 截断后的数值数组 ---
# (与 Python demosaic_bayer 逐像素等价: 边界只用界内同色邻居, 无 padding)
demosaic_bayer <- function(cfa, pattern) {
    h <- nrow(cfa)
    w <- ncol(cfa)
    cfa_f <- cfa + 0.0
    cmap <- color_map(pattern, h, w)
    is_g <- matrix(as.numeric(cmap == "G"), h, w)
    is_r <- matrix(as.numeric(cmap == "R"), h, w)
    is_b <- matrix(as.numeric(cmap == "B"), h, w)

    kernel8 <- expand.grid(dy = -1:1, dx = -1:1)
    kernel8 <- kernel8[!(kernel8$dy == 0 & kernel8$dx == 0), , drop = FALSE]
    kernel8 <- lapply(seq_len(nrow(kernel8)), function(i) c(kernel8$dy[i], kernel8$dx[i]))

    # G: R/B 位置 = 同色 (正交) G 邻域平均
    g_avg <- gather_avg(cfa_f, h, w, is_g, kernel8)
    g <- ifelse(is_g == 1, cfa_f, g_avg)

    # R: 缺失位置 = 同色邻域平均 * (G_here / G_at_R_neighbors) 校正
    r_avg <- gather_avg(cfa_f, h, w, is_r, kernel8)
    g_at_r <- gather_avg(g, h, w, is_r, kernel8)
    r <- ifelse(is_r == 1, cfa_f, r_avg * (g / pmax(g_at_r, 1e-9)))

    # B: 对称
    b_avg <- gather_avg(cfa_f, h, w, is_b, kernel8)
    g_at_b <- gather_avg(g, h, w, is_b, kernel8)
    b <- ifelse(is_b == 1, cfa_f, b_avg * (g / pmax(g_at_b, 1e-9)))

    r <- pmin(255, pmax(0, r))
    g <- pmin(255, pmax(0, g))
    b <- pmin(255, pmax(0, b))
    arr <- array(0, dim = c(h, w, 3))
    arr[, , 1] <- floor(r)  # 模拟 numpy astype(uint8) 截断
    arr[, , 2] <- floor(g)
    arr[, , 3] <- floor(b)
    arr
}

# --- 楔形区域缺陷修复: 区域内像素与带外同相位参考差>threshold 则替换 ---
# region_cols: 疑似缺陷列 (0 基, 如 502:516); y_lo/y_hi: 检查行 [y_lo, y_hi) (0 基)
# 返回 list(cfa=, replaced=, count=)
fix_defect_region <- function(cfa, region_cols, y_lo, y_hi, threshold = 55.0) {
    cfa <- cfa + 0.0
    h <- nrow(cfa)
    w <- ncol(cfa)
    replaced <- matrix(FALSE, h, w)
    count <- 0L
    y_lo_r <- y_lo + 1L          # 0 基 -> R 1 基
    y_hi_r <- min(y_hi, h)       # Python range(y_lo, min(y_hi, h)) 的上界
    cols <- as.integer(region_cols) + 1L
    if (y_lo_r > y_hi_r) return(list(cfa = cfa, replaced = replaced, count = count))
    for (y in y_lo_r:y_hi_r) {
        for (x in cols) {
            lv <- NULL
            rv <- NULL
            lx <- x - 2L
            while (lx >= 1L) {
                if (!(lx %in% cols)) { lv <- cfa[y, lx]; break }
                lx <- lx - 2L
            }
            rx <- x + 2L
            while (rx <= w) {
                if (!(rx %in% cols)) { rv <- cfa[y, rx]; break }
                rx <- rx + 2L
            }
            refs <- c(lv, rv)    # NULL 自动剔除
            if (length(refs) == 0) next
            ref <- mean(refs)
            if (abs(cfa[y, x] - ref) > threshold) {
                cfa[y, x] <- ref
                replaced[y, x] <- TRUE
                count <- count + 1L
            }
        }
    }
    list(cfa = cfa, replaced = replaced, count = count)
}

# --- 散点死点修复: 与同色 4-邻域 (上下左右, 间隔 2) 中值差>threshold 则替换 ---
# skip: 逻辑掩码, TRUE 的像素不处理 (如区域缺陷修复区)
# 返回 list(cfa=, count=)
fix_dead_pixels <- function(cfa, threshold = 60.0, skip = NULL) {
    cfa <- cfa + 0.0
    h <- nrow(cfa)
    w <- ncol(cfa)
    if (is.null(skip)) skip <- matrix(FALSE, h, w)
    count <- 0L
    if (h < 5 || w < 5) return(list(cfa = cfa, count = count))
    for (y in 3:(h - 2)) {       # Python range(2, h-2): y=2..h-3 -> R 3..h-2
        for (x in 3:(w - 2)) {
            if (skip[y, x]) next
            nbrs <- numeric(0)
            for (off in list(c(-2, 0), c(2, 0), c(0, -2), c(0, 2))) {
                yy <- y + off[1]
                xx <- x + off[2]
                if (yy >= 1L && yy <= h && xx >= 1L && xx <= w && !skip[yy, xx]) {
                    nbrs <- c(nbrs, cfa[yy, xx])
                }
            }
            if (length(nbrs) < 2) next
            med <- median(nbrs)
            if (abs(cfa[y, x] - med) > threshold) {
                cfa[y, x] <- med
                count <- count + 1L
            }
        }
    }
    list(cfa = cfa, count = count)
}

# --- 上/下半窗分相位去马赛克 -> 拼接 -> 灰度世界 WB -> 伽马, 返回 (480,640,3) 0..255 ---
render_stitched <- function(cfa, upper_pattern = "BGGR", lower_pattern = "GRBG",
                            r_gain = NULL, b_gain = NULL,
                            gamma = 0.85, b_extra = 0.93) {
    up <- demosaic_bayer(cfa[1:240, , drop = FALSE], upper_pattern) + 0.0
    lo <- demosaic_bayer(cfa[241:480, , drop = FALSE], lower_pattern) + 0.0
    stitched <- array(0, dim = c(480, 640, 3))
    stitched[1:240, , ] <- up
    stitched[241:480, , ] <- lo
    if (is.null(r_gain)) r_gain <- mean(stitched[, , 2]) / mean(stitched[, , 1])
    if (is.null(b_gain)) b_gain <- mean(stitched[, , 2]) / mean(stitched[, , 3])
    img <- stitched
    img[, , 1] <- img[, , 1] * r_gain
    img[, , 3] <- img[, , 3] * b_gain * b_extra
    img[, , 1] <- pmin(255, pmax(0, img[, , 1]))   # 逐通道 clip, 保持 array 形状
    img[, , 2] <- pmin(255, pmax(0, img[, , 2]))
    img[, , 3] <- pmin(255, pmax(0, img[, , 3]))
    img <- 255 * (img / 255)^gamma
    floor(img)                   # 模拟 numpy astype(uint8) 截断
}

# --- 完整管线: [可选 g_map] -> 楔形区域修复 -> 散点死点修复 -> 渲染 ---
# raw: 480x640 整数矩阵 (原始字节)。
# use_gmap: TRUE 时先做 6-bit 位序解码 (旧 COM15=0xD0 模式), FALSE 直通。
# 返回 list(rgb=, stats=)
pipeline <- function(raw, use_gmap = FALSE, map = "old",
                     region_cols = 502:516, region_y = c(60, 160),
                     dead_threshold = 60.0, region_threshold = 55.0,
                     fix_region = TRUE, fix_dead = TRUE,
                     upper_pattern = "GRBG", lower_pattern = "BGGR",
                     r_gain = NULL, b_gain = NULL, gamma = 0.85, b_extra = 0.93) {
    cfa <- if (use_gmap) g_map(raw, map) + 0.0 else raw + 0.0
    stats <- list()
    replaced <- matrix(FALSE, nrow(cfa), ncol(cfa))
    if (fix_region) {
        res <- fix_defect_region(cfa, region_cols, region_y[1], region_y[2], region_threshold)
        cfa <- res$cfa
        replaced <- res$replaced
        stats$region_replaced <- as.integer(res$count)
        stats$region_skipped <- as.integer(sum(replaced))
    }
    if (fix_dead) {
        res2 <- fix_dead_pixels(cfa, dead_threshold, skip = replaced)
        cfa <- res2$cfa
        stats$dead_replaced <- as.integer(res2$count)
    }
    rgb <- render_stitched(cfa, upper_pattern, lower_pattern, r_gain, b_gain, gamma, b_extra)
    list(rgb = rgb, stats = stats)
}

# --- CLI 参数解析 (与 Python argparse 语义对齐) ---
parse_range <- function(s, name) {
    parts <- as.integer(strsplit(s, "-", fixed = TRUE)[[1]])
    if (length(parts) != 2 || is.na(parts[1]) || is.na(parts[2]) ||
        parts[1] < 0 || parts[2] < parts[1]) {
        stop(sprintf("invalid --%s: %s", name, s), call. = FALSE)
    }
    list(cols = parts[1]:parts[2], y = c(parts[1], parts[2] + 1L))
}

main <- function(argv) {
    args <- argv
    get_flag <- function(flag) {
        i <- match(flag, args)
        if (is.na(i)) FALSE else { args[i] <<- NA; TRUE }
    }
    get_val <- function(flag, default) {
        i <- match(flag, args)
        if (is.na(i)) default else { v <- args[i + 1L]; args[i] <<- NA; args[i + 1L] <<- NA; v }
    }

    inputs <- args[!is.na(args) & !grepl("^-", args)]
    if (length(inputs) < 1) stop("usage: Rscript bayer_pipeline.R input.raw -o out.png", call. = FALSE)
    input <- inputs[1]
    output <- get_val("-o", NULL)
    if (is.na(output)) output <- get_val("--output", NULL)
    if (is.na(output)) stop("-o/--output required", call. = FALSE)

    no_region <- get_flag("--no-region-fix")
    no_dead <- get_flag("--no-dead-fix")
    use_gmap <- get_flag("--use-gmap")
    map <- get_val("--map", "old")
    if (!map %in% c("old", "new")) stop("--map must be old or new", call. = FALSE)
    region_cols_str <- get_val("--region-cols", "502-516")
    region_y_str <- get_val("--region-y", "60-160")
    dead_threshold <- as.numeric(get_val("--dead-threshold", "60"))
    region_threshold <- as.numeric(get_val("--region-threshold", "55"))
    r_gain_str <- get_val("--r-gain", NA)
    b_gain_str <- get_val("--b-gain", NA)
    b_extra <- as.numeric(get_val("--b-extra", "0.93"))
    gamma <- as.numeric(get_val("--gamma", "0.85"))
    upper_pattern <- get_val("--upper", "GRBG")
    lower_pattern <- get_val("--lower", "BGGR")

    rc <- parse_range(region_cols_str, "region-cols")
    ry <- parse_range(region_y_str, "region-y")
    r_gain <- if (is.na(r_gain_str)) NULL else as.numeric(r_gain_str)
    b_gain <- if (is.na(b_gain_str)) NULL else as.numeric(b_gain_str)

    # 读 raw: 480x640 字节, 行主序
    con <- file(input, "rb")
    on.exit(close(con))
    bytes <- readBin(con, "raw", n = 480 * 640)
    if (length(bytes) < 480 * 640) stop(sprintf("%s: %d B < 需要 %d B (480x640)", input, length(bytes), 480 * 640), call. = FALSE)
    raw <- matrix(as.integer(bytes[1:(480 * 640)]), nrow = 480, ncol = 640, byrow = TRUE)

    res <- pipeline(raw, use_gmap = use_gmap, map = map, region_cols = rc$cols, region_y = ry$y,
                    dead_threshold = dead_threshold, region_threshold = region_threshold,
                    fix_region = !no_region, fix_dead = !no_dead,
                    upper_pattern = upper_pattern, lower_pattern = lower_pattern,
                    r_gain = r_gain, b_gain = b_gain, gamma = gamma, b_extra = b_extra)

    # 写 PNG (8-bit RGB, 与 PIL 输出等价)
    writePNG(res$rgb / 255, target = output)
    cat(sprintf("%s: %dx%d saved\n", output, ncol(res$rgb), nrow(res$rgb)))
    st <- res$stats
    if (length(st) > 0) {
        cat(sprintf("  region_replaced=%d (skip %d) dead_replaced=%d\n",
                    st$region_replaced, st$region_skipped, st$dead_replaced))
    }
    invisible(res)
}

# --- 模块守卫: 仅命令行运行时执行 main ---
if (sys.nframe() == 0 && !interactive()) {
    main(commandArgs(trailingOnly = TRUE))
}
