#!/usr/bin/env Rscript
# OV7670 Raw Bayer 320x240 pipeline: dead pixel fix -> demosaic -> WB -> gamma.
#
# Usage:
#   Rscript bayer_pipeline.R input.raw -o out.png
#   Rscript bayer_pipeline.R input.raw -o out.png --no-dead-fix

suppressPackageStartupMessages(library(png))

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

color_map <- function(pattern, h, w) {
    yy <- (0:(h - 1)) %% 2
    xx <- (0:(w - 1)) %% 2
    idx <- outer(yy * 2, xx, `+`) + 1L
    chars <- strsplit(pattern, "")[[1]]
    matrix(chars[idx], h, w)
}

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

    g_avg <- gather_avg(cfa_f, h, w, is_g, kernel8)
    g <- ifelse(is_g == 1, cfa_f, g_avg)

    r_avg <- gather_avg(cfa_f, h, w, is_r, kernel8)
    g_at_r <- gather_avg(g, h, w, is_r, kernel8)
    r <- ifelse(is_r == 1, cfa_f, r_avg * (g / pmax(g_at_r, 1e-9)))

    b_avg <- gather_avg(cfa_f, h, w, is_b, kernel8)
    g_at_b <- gather_avg(g, h, w, is_b, kernel8)
    b <- ifelse(is_b == 1, cfa_f, b_avg * (g / pmax(g_at_b, 1e-9)))

    r <- pmin(255, pmax(0, r))
    g <- pmin(255, pmax(0, g))
    b <- pmin(255, pmax(0, b))
    arr <- array(0, dim = c(h, w, 3))
    arr[, , 1] <- floor(r)
    arr[, , 2] <- floor(g)
    arr[, , 3] <- floor(b)
    arr
}

fix_dead_pixels <- function(cfa, threshold = 60.0) {
    cfa <- cfa + 0.0
    h <- nrow(cfa)
    w <- ncol(cfa)
    count <- 0L
    if (h < 5 || w < 5) return(list(cfa = cfa, count = count))
    for (y in 3:(h - 2)) {
        for (x in 3:(w - 2)) {
            nbrs <- numeric(0)
            for (off in list(c(-2, 0), c(2, 0), c(0, -2), c(0, 2))) {
                yy <- y + off[1]
                xx <- x + off[2]
                if (yy >= 1L && yy <= h && xx >= 1L && xx <= w) {
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

render <- function(cfa, pattern = "RGGB", r_gain = NULL, b_gain = NULL,
                   gamma = 0.85, b_extra = 0.93) {
    rgb <- demosaic_bayer(cfa, pattern) + 0.0
    if (is.null(r_gain)) r_gain <- mean(rgb[, , 2]) / mean(rgb[, , 1])
    if (is.null(b_gain)) b_gain <- mean(rgb[, , 2]) / mean(rgb[, , 3])
    img <- rgb
    img[, , 1] <- img[, , 1] * r_gain
    img[, , 3] <- img[, , 3] * b_gain * b_extra
    img[, , 1] <- pmin(255, pmax(0, img[, , 1]))
    img[, , 2] <- pmin(255, pmax(0, img[, , 2]))
    img[, , 3] <- pmin(255, pmax(0, img[, , 3]))
    img <- 255 * (img / 255)^gamma
    floor(img)
}

fix_zero_columns <- function(cfa) {
    h <- nrow(cfa)
    w <- ncol(cfa)
    if (w < 2) return(cfa)
    for (r in seq_len(h)) {
        row <- cfa[r, ]
        i <- 1
        while (i <= w) {
            if (row[i] != 0) { i <- i + 1; next }
            lo <- i
            while (i <= w && row[i] == 0) i <- i + 1
            hi <- i - 1L
            left_val <- NA; left_idx <- lo - 1L
            while (left_idx >= 1L) {
                if (row[left_idx] != 0) { left_val <- row[left_idx]; break }
                left_idx <- left_idx - 1L
            }
            right_val <- NA; right_idx <- hi + 1L
            while (right_idx <= w) {
                if (row[right_idx] != 0) { right_val <- row[right_idx]; break }
                right_idx <- right_idx + 1L
            }
            if (!is.na(left_val) && !is.na(right_val)) {
                span <- right_idx - left_idx
                for (j in lo:hi) {
                    t_val <- (j - left_idx) / span
                    row[j] <- left_val * (1 - t_val) + right_val * t_val
                }
            } else if (!is.na(left_val)) {
                row[lo:hi] <- left_val
            } else if (!is.na(right_val)) {
                row[lo:hi] <- right_val
            }
        }
        cfa[r, ] <- row
    }
    cfa
}

pipeline <- function(raw, dead_threshold = 60.0, fix_dead = TRUE,
                     pattern = "RGGB",
                     r_gain = NULL, b_gain = NULL, gamma = 0.85, b_extra = 0.93) {
    cfa <- fix_zero_columns(raw + 0.0)
    stats <- list()
    if (fix_dead) {
        res <- fix_dead_pixels(cfa, dead_threshold)
        cfa <- res$cfa
        stats$dead_replaced <- as.integer(res$count)
    }
    rgb <- render(cfa, pattern, r_gain, b_gain, gamma, b_extra)
    list(rgb = rgb, stats = stats)
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

    no_dead <- get_flag("--no-dead-fix")
    dead_threshold <- as.numeric(get_val("--dead-threshold", "60"))
    r_gain_str <- get_val("--r-gain", NA)
    b_gain_str <- get_val("--b-gain", NA)
    b_extra <- as.numeric(get_val("--b-extra", "0.93"))
    gamma <- as.numeric(get_val("--gamma", "0.85"))

    r_gain <- if (is.na(r_gain_str)) NULL else as.numeric(r_gain_str)
    b_gain <- if (is.na(b_gain_str)) NULL else as.numeric(b_gain_str)

    con <- file(input, "rb")
    on.exit(close(con))
    bytes <- readBin(con, "raw", n = 240 * 320)
    if (length(bytes) < 240 * 320) stop(sprintf("%s: %d B < need %d B (240x320)", input, length(bytes), 240 * 320), call. = FALSE)
    raw <- matrix(as.integer(bytes[1:(240 * 320)]), nrow = 240, ncol = 320, byrow = TRUE)

    res <- pipeline(raw, dead_threshold = dead_threshold, fix_dead = !no_dead,
                    r_gain = r_gain, b_gain = b_gain, gamma = gamma, b_extra = b_extra)

    writePNG(res$rgb / 255, target = output)
    cat(sprintf("%s: %dx%d saved\n", output, ncol(res$rgb), nrow(res$rgb)))
    st <- res$stats
    if (length(st) > 0) {
        cat(sprintf("  dead_replaced=%d\n", st$dead_replaced))
    }
    invisible(res)
}

if (sys.nframe() == 0 && !interactive()) {
    main(commandArgs(trailingOnly = TRUE))
}
