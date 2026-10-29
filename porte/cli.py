"""Porte command-line interface."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import (bench, build, cache, experiment, macho, profile, specialize,
               util, verify)
from .pe import experiment as pe_experiment
from .pe import pipeline as pe_pipeline


def _cmd_build(args) -> int:
    paths = build.build_all(force=args.force)
    for arch, p in paths.items():
        print(f"{arch}: {p['app']}")
    return 0


def _cmd_analyze(args) -> int:
    path = Path(args.binary)
    blks = macho.blocks(path)
    doc = {
        "path": str(path), "arch": macho.arch_of(path),
        "sha256": util.sha256_file(path),
        "blocks": len(blks),
        "instructions": sum(b.ninstr for b in blks),
        "indirect_blocks": sum(1 for b in blks if b.ends_indirect),
        "blocks_detail": [
            {"start": hex(b.start), "function": b.func, "instructions": b.ninstr,
             "ends_indirect": b.ends_indirect,
             "last": b.insns[-1].text if b.insns else ""}
            for b in blks
        ],
    }
    print(json.dumps({k: v for k, v in doc.items() if k != "blocks_detail"},
                     indent=2))
    if args.json:
        util.write_json(Path(args.json), doc)
    return 0


def _cmd_profile(args) -> int:
    names = list(profile.WORKLOADS) if args.workload == "all" else [args.workload]
    for name in names:
        prof = profile.profile_workload(name, arch=args.arch)
        out = util.PROFILES / f"{name}.json"
        util.write_json(out, prof)
        s = prof["sampling"]
        print(f"{name}: {s['samples']} samples ({s['effective_hz']} Hz), "
              f"{s['inside_module']} in module, {s['unmapped']} unattributed; "
              f"facts={prof['facts']} -> {out}")
    return 0


def _cmd_specialize(args) -> int:
    names = list(profile.WORKLOADS)
    profiles = {}
    for name in names:
        path = util.PROFILES / f"{name}.json"
        if path.exists():
            profiles[name] = util.read_json(path)
    if not profiles:
        print("no profiles found; run `porte profile` first", file=sys.stderr)
        return 2
    plans = specialize.plan_from_profiles(profiles)
    if not plans:
        print("no plan justified by the profiles", file=sys.stderr)
        return 3
    out_dir = Path(args.out) if args.out else util.BUILD / "spec" / "manual"
    artifacts = specialize.compile_plans(plans, args.arch, out_dir)
    print(json.dumps(artifacts, indent=2))
    return 0


def _cmd_verify(args) -> int:
    verdict = verify.verify_candidate(Path(args.dylib))
    util.write_json(util.RESULTS / "verification.json", verdict)
    for case in verdict["cases"]:
        mark = "ok" if case["ok"] else "FAIL"
        print(f"{mark:4s} {case['workload']:20s} "
              f"fast={case['candidate']['fast']} "
              f"fallback={case['candidate']['fallback']} "
              f"digest={case['candidate']['out_digest']}")
    print(f"verdict: {'PASS' if verdict['passed'] else 'FAIL'}")
    return 0 if verdict["passed"] else 1


def _cmd_run(args) -> int:
    arms = {
        "baseline": ("x86_64", build.paths_for("x86_64")["generic"]),
        "native": ("arm64", build.paths_for("arm64")["generic"]),
        "spec": ("arm64", Path(args.dylib) if args.dylib else None),
    }
    arch, kernel = arms[args.arm]
    if kernel is None:
        print("--dylib is required for --arm spec", file=sys.stderr)
        return 2
    run = profile.run_app(arch, kernel, args.workload,
                          ledger=Path(args.ledger) if args.ledger else None)
    print(json.dumps({
        "arm": args.arm, "arch": arch, "workload": args.workload,
        "impl": run.impl, "ret": run.ret, "out_digest": run.out_digest,
        "kernel_ns": run.kernel_ns, "wall_ns": run.wall_ns,
        "maxrss_bytes": run.maxrss, "transitions": run.transitions,
        "entries": run.entries, "fast": run.fast, "fallback": run.fallback,
    }, indent=2))
    return 0


def _cmd_bench(args) -> int:
    result = bench.run_bench(Path(args.dylib), args.workload or
                             list(profile.WORKLOADS), repeats=args.repeats)
    util.write_json(util.RESULTS / "bench.json", result)
    for doc in result["workloads"].values():
        print(bench.format_table(doc))
    return 0


def _cmd_experiment(args) -> int:
    result = experiment.run(repeats=args.repeats, force_build=args.force_build)
    print(experiment.summary_text(result))
    return 0


def _cmd_selftest(args) -> int:
    result = verify.mutation_test()
    print(json.dumps({k: v for k, v in result.items() if k != "verdict"},
                     indent=2))
    return 0 if result["oracle_rejected"] else 1


def _cmd_warm(args) -> int:
    report = experiment.warm(args.key or None)
    out = {k: v for k, v in report.items() if k != "smoke"}
    if report.get("bench"):
        b = report["bench"]
        out["bench_summary"] = {
            "workload": b["workload"],
            "speedup_vs_native_inline": b["speedup_vs_native_inline"],
            "speedup_vs_native_generic": b["speedup_vs_native_generic"],
            "speedup_vs_translated_baseline": b["speedup_vs_translated_baseline"],
            "kernel_ns_median": {
                arm: s["kernel_ns"]["median"] for arm, s in b["summary"].items()
            },
        }
        del out["bench"]
    print(json.dumps(out, indent=2))
    return 0 if report.get("smoke_passed") else 1


def _cmd_cache(args) -> int:
    if args.action == "list":
        for m in sorted(util.CACHE.glob("*/manifest.json")):
            doc = util.read_json(m)
            print(f"{doc['key']}  {doc['env']['transform']}  "
                  f"verified={doc['verification']['passed']}  {m.parent}")
        return 0
    if args.action == "show":
        doc = cache.load(args.key) or cache.latest()
        if not doc:
            print("no cache entries", file=sys.stderr)
            return 2
        print(json.dumps(doc, indent=2))
        return 0
    print(f"unknown action {args.action}", file=sys.stderr)
    return 2


def _cmd_pe(args) -> int:
    from .pe.pipeline import (CORPUS, describe, find_cached, load_and_check,
                              optimize, run_baseline, run_with_artifact,
                              selftest, verify_cases)
    pe_path = Path(args.exe)
    if args.action == "analyze":
        print(json.dumps(describe(pe_path), indent=2))
        return 0
    if args.action == "optimize":
        res = optimize(pe_path, profile_args=args.workload and [args.workload],
                       force=args.force)
        if res.get("cached"):
            print(f"cache hit {res['manifest']['key']}")
            return 0
        if not res.get("ok"):
            print(json.dumps({k: v for k, v in res.items() if k != 'artifact'},
                             indent=2, default=str))
            return 1
        m = res["manifest"]
        print(f"admitted {m['key']}  cases={len(m['verification']['cases'])} "
              f"pins={m['artifact']['pinned_sites']} "
              f"build={m['artifact'].get('build_seconds'):.2f}s "
              f"clang={m['artifact'].get('clang_seconds'):.2f}s")
        return 0
    if args.action == "verify":
        manifest = find_cached(pe_path)
        if manifest is None:
            print("no cached artifact for this image", file=sys.stderr)
            return 2
        v = verify_cases(pe_path, manifest["artifact"], CORPUS)
        bad = [c for c in v["results"] if not c["match"]]
        print(f"{len(v['cases'])} cases, {len(bad)} failures")
        for c in bad[:5]:
            print(f"  argv={c['argv']} {pe_pipeline.diff_summary(c)}")
        return 0 if v["passed"] else 1
    if args.action == "run":
        manifest = find_cached(pe_path)
        if manifest is not None and not args.baseline:
            obs = run_with_artifact(pe_path, manifest["artifact"], [args.workload])
        else:
            obs = run_baseline(pe_path, [args.workload])
        print(json.dumps(obs.to_dict(), indent=2, default=str))
        return 0
    if args.action == "bench":
        manifest = find_cached(pe_path)
        if manifest is None:
            print("no cached artifact for this image", file=sys.stderr)
            return 2
        print(json.dumps(pe_pipeline.bench(pe_path, manifest, [args.workload],
                                           repeats=args.repeats), indent=2))
        return 0
    if args.action == "selftest":
        print(json.dumps(selftest(pe_path, corpus=[args.workload, "64"],
                                  profile_args=[args.workload]), indent=2))
        return 0
    if args.action == "warm":
        reuse = load_and_check(pe_path)
        if reuse is None:
            print("no reusable artifact (miss, layout change or smoke failure)")
            return 1
        print(json.dumps({"key": reuse["manifest"]["key"],
                          "smoke": reuse["smoke"]["passed"],
                          "check_seconds": reuse["check_seconds"]}, indent=2))
        return 0
    if args.action == "experiment":
        report = pe_experiment.run(repeats=args.repeats)
        print(pe_experiment.summarise(report))
        return 0
    print(f"unknown action {args.action}", file=sys.stderr)
    return 2


def _cmd_frame(args) -> int:
    from .frame import experiment as frame_experiment

    report = frame_experiment.run(repeats=args.repeats,
                                  with_scaling=not args.no_scaling)
    print(frame_experiment.table(report))
    print()
    print(frame_experiment.breakdown(report))
    cold = report["shader_cold"]
    print(f"\ncold shader compile: native {cold['native_arm64_ms']:.1f} ms, "
          f"host {cold['porte_host_ms']:.1f} ms")
    return 0


def _cmd_runtime(args) -> int:
    from .backends import describe, wine as wine_backend
    from .backends import bench as bench_mod
    from .backends import verify as verify_mod

    if args.action == "list":
        rows = describe()
        print(f"{'backend':8s} {'available':10s} {'version':14s} license")
        for row in rows:
            print(f"{row['id']:8s} {str(row['available']):10s} "
                  f"{str(row['version'])[:14]:14s} {row['license']}")
            if row.get("path"):
                print(f"         runtime: {row['path']}")
            for warning in row.get("warnings") or []:
                print(f"         note: {warning}")
            for reason in row.get("reasons") or []:
                print(f"         problem: {reason}")
        print()
        print("Porte does not download or install a compatibility runtime. "
              "Install Wine yourself (for example Wine Staging from the "
              "WineHQ macOS builds) and it is discovered automatically, or "
              f"point {wine_backend.ROOT_ENV} at it.")
        return 0

    if args.action == "verify":
        if args.backend == "engine":
            from .backends import engine as engine_backend

            status = engine_backend.backend().status()
            print(f"engine: {'available' if status.available else 'unavailable'}")
            for reason in status.reasons:
                print(f"  problem: {reason}")
            return 0 if status.available else 1
        status = wine_backend.backend().status()
        ok = status.available
        print(f"wine: {'available' if ok else 'unavailable'} "
              f"{status.version or ''}")
        if status.path:
            print(f"  binary:  {status.path}")
        print(f"  license: {status.license}")
        print(f"  Rosetta 2: {'present' if status.rosetta_available else 'missing'}")
        for warning in status.warnings:
            print(f"  note: {warning}")
        for reason in status.reasons:
            print(f"  problem: {reason}")
        if args.smoke and status.available:
            runtime = wine_backend.select()
            prefix = wine_backend.ensure_bottle(runtime, "verify-smoke")
            proc = wine_backend.run_command(
                [str(runtime.wine), "cmd", "/c", "echo porte-runtime-ok"],
                env=wine_backend.clean_env(prefix), timeout=300)
            ok = ok and "porte-runtime-ok" in (proc.stdout or "")
            print(f"  guest program: {proc.stdout.strip() or proc.stderr.strip()[:200]}")
        return 0 if ok else 1

    if args.action == "verify-e2e":
        verify_mod.require_fixtures()
        wanted = args.only or ["fixture", "notepadpp", "7zip"]
        reports = {}
        exit_code = 0
        if "fixture" in wanted:
            report = verify_mod.verify_gui_fixture(keep=args.keep)
            reports["fixture"] = verify_mod.collect(report)
            exit_code |= 0 if report["passed"] else 1
            print(f"fixture: {'PASS' if report['passed'] else 'FAIL'} "
                  f"({len(report['checks'])} checks)")
            for failure in report["failures"]:
                print(f"  FAIL {failure['name']}: {failure['detail']}")
        third = verify_mod.THIRD_PARTY
        npp = third / "npp" / "notepad++.exe"
        if "notepadpp" in wanted:
            if npp.exists():
                report = verify_mod.verify_portable_gui(
                    npp, "Notepad++", keep=args.keep,
                    accuracy_note="Notepad++ portable, third-party")
                reports["notepadpp"] = verify_mod.collect(report)
                exit_code |= 0 if report["passed"] else 1
                print(f"notepad++: {'PASS' if report['passed'] else 'FAIL'} "
                      f"({len(report['checks'])} checks)")
                for failure in report["failures"]:
                    print(f"  FAIL {failure['name']}: {failure['detail']}")
            else:
                print("notepad++: skipped (run fixtures/win32/fetch_third_party.py)")
                reports["notepadpp"] = {"skipped": True}
        msi = third / "7z2605-x64.msi"
        if "7zip" in wanted:
            if msi.exists():
                report = verify_mod.verify_msi(
                    msi, keep=args.keep, launch_title="7-Zip",
                    cli_exe=r"C:\Program Files\7-Zip\7z.exe", cli_args=["i"])
                reports["7zip"] = verify_mod.collect(report)
                exit_code |= 0 if report["passed"] else 1
                print(f"7-zip (msi): {'PASS' if report['passed'] else 'FAIL'} "
                      f"({len(report['checks'])} checks)")
                for failure in report["failures"]:
                    print(f"  FAIL {failure['name']}: {failure['detail']}")
            else:
                print("7-zip: skipped (run fixtures/win32/fetch_third_party.py)")
                reports["7zip"] = {"skipped": True}
        from . import util as util_mod

        util_mod.write_json(util_mod.RESULTS / "runtime_verification.json",
                            reports)
        return exit_code

    if args.action == "bench":
        report = bench_mod.measure(repeats=args.repeats,
                                   iterations=args.iterations,
                                   include_gui_startup=not args.no_gui)
        print(bench_mod.summarise(report))
        path = bench_mod.write(report)
        print(f"\nwrote {path}")
        return 0

    print(f"unknown runtime action {args.action}", file=sys.stderr)
    return 2


def _resolve_product(identifier: str) -> str:
    from .app import environment

    products = environment.list_products()
    for product in products:
        if identifier in (product.get("product_key"), product.get("name")):
            return product["product_key"]
    for product in products:
        if identifier.lower() in (product.get("name") or "").lower():
            return product["product_key"]
    raise KeyError(f"no installed product matches {identifier!r}")


def _cmd_app(args) -> int:
    from .app import bundle, environment, install

    if args.action == "import":
        package = Path(args.package).expanduser()
        suffix = package.suffix.lower()
        common = dict(accept=args.accept, force=args.force,
                      dest=Path(args.dest).expanduser() if args.dest else None,
                      generate_bundles=not args.no_bundles)
        preferred = args.runtime or "auto"
        try:
            if suffix == ".exe":
                from .backends import choose_for_install

                backend, _document = choose_for_install(package, preferred)
                kwargs = dict(name=args.name, arguments=args.args or None,
                              accept=args.accept, force=args.force,
                              dest=common["dest"],
                              include_dlls=not args.no_dlls)
                if backend.id == "wine":
                    kwargs["include_siblings"] = not args.no_siblings
                result = backend.install(package, **kwargs)
            elif suffix == ".msi":
                from .app.msi import install as msi_install
                from .backends import wine as wine_backend

                use_wine = preferred == "wine"
                if preferred in ("auto", None, ""):
                    try:
                        msi_install.inspect_package(package)
                    except Exception:
                        use_wine = wine_backend.backend().status().available
                    else:
                        info = msi_install.inspect_package(package)
                        use_wine = (not info.get("supported") and
                                    wine_backend.backend().status().available)
                if use_wine:
                    result = wine_backend.backend().install_msi(
                        package, dest=common["dest"], force=args.force,
                        accept=args.accept)
                else:
                    result = msi_install.install(
                        package, ignore_custom_actions=args.allow_custom_actions,
                        **common)
            elif suffix in (".msix", ".appx"):
                from .app import msix

                result = msix.install(package, **common)
            elif suffix in (".bat", ".cmd"):
                print("batch files are not supported: Porte does not implement "
                      "cmd.exe. Package the program it starts, or wrap it in a "
                      "Windows PE.", file=sys.stderr)
                return 2
            else:
                print(f"unsupported package format {suffix or package.name}",
                      file=sys.stderr)
                return 2
        except (NotImplementedError, PermissionError, FileExistsError,
                FileNotFoundError, KeyError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        except Exception as exc:
            from .app.msi.install import UnsupportedPackage
            from .app.msix import PackageError

            if isinstance(exc, (UnsupportedPackage, PackageError)):
                print(str(exc), file=sys.stderr)
                return 2
            raise
        print(json.dumps(result, indent=2))
        if result.get("bundles"):
            print("\nApplications installed:")
            for entry in result["bundles"]:
                print(f"  {entry['app']}")
        for entry in result.get("skipped_executables", []):
            print(f"  skipped {entry['file']}: missing "
                  f"{', '.join(entry['missing'][:4])}")
        return 0
    if args.action == "products":
        products = environment.list_products()
        if not products:
            print("no Windows products installed")
            return 0
        print(f"{'product':44s} {'kind':5s} {'version':10s} apps  name")
        for product in products:
            print(f"{product['product_key']:44s} {product['kind']:5s} "
                  f"{product.get('version', '')[:10]:10s} "
                  f"{len(product.get('bundles', [])):4d}  {product.get('name')}")
        return 0
    if args.action == "package-info":
        package = Path(args.package).expanduser()
        try:
            if package.suffix.lower() == ".msi":
                from .app.msi.install import inspect_package
            elif package.suffix.lower() in (".msix", ".appx"):
                from .app.msix import inspect_package
            else:
                print("package-info takes an .msi, .msix or .appx file",
                      file=sys.stderr)
                return 2
            info = inspect_package(package)
        except Exception as exc:  # malformed packages are reported, not raised
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps(info, indent=2))
        return 0
    if args.action in ("uninstall", "repair"):
        try:
            key = _resolve_product(args.product)
        except KeyError as exc:
            print(exc.args[0] if exc.args else str(exc), file=sys.stderr)
            return 2
        manifest = environment.read_manifest(key) or {}
        if manifest.get("kind") == "msi":
            from .app.msi import install as engine
        else:
            from .app import msix as engine
        try:
            result = engine.uninstall(key) if args.action == "uninstall" \
                else engine.repair(key)
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps(result, indent=2))
        return 0
    if args.action == "install":
        try:
            exe_path = Path(args.exe)
            preferred = args.runtime or "auto"
            if preferred == "wine":
                from .backends import wine as wine_backend

                result = wine_backend.backend().install(
                    exe_path, name=args.name,
                    dest=Path(args.dest).expanduser() if args.dest else None,
                    arguments=args.args or None, accept=args.accept,
                    force=args.force, include_dlls=not args.no_dlls,
                    include_siblings=not args.no_siblings)
            else:
                result = install.install(
                    exe_path, name=args.name, bundle_id=args.bundle_id,
                    dest=Path(args.dest).expanduser() if args.dest else None,
                    arguments=args.args or None, accept=args.accept,
                    force=args.force, include_dlls=not args.no_dlls)
        except (NotImplementedError, PermissionError, FileExistsError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        except Exception as exc:
            from .backends.base import BackendError

            if isinstance(exc, BackendError):
                print(str(exc), file=sys.stderr)
                return 2
            raise
        print(json.dumps(result, indent=2))
        print(f"\nInstalled {result['app']}\n"
              f"Launch it with: open \"{result['app']}\"")
        return 0
    if args.action == "list":
        rows = []
        for base in (Path.home() / "Applications", Path("/Applications")):
            if not base.exists():
                continue
            for app in sorted(base.glob("*.app")):
                try:
                    info = bundle.inspect_app(app)
                except Exception:
                    continue
                if (info.get("bundle_id") or "").startswith("com.notomer.porte.windows."):
                    rows.append(info)
        if not rows:
            print("no Porte application bundles installed")
            return 0
        print(f"{'name':24s} {'bundle id':46s} {'profile':12s} runs  path")
        for info in rows:
            print(f"{info['name'][:24]:24s} {info['bundle_id']:46s} "
                  f"{(info.get('profile') or '-'):12s} {info['runs']:4d}  {info['app']}")
        return 0
    if args.action == "inspect":
        try:
            app = bundle.find_app(args.app)
        except FileNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps(bundle.inspect_app(app), indent=2))
        return 0
    if args.action == "launch":
        res = install.launch(Path(args.app))
        print(json.dumps(res, indent=2))
        return 0 if res["opened"] else 1
    if args.action == "remove":
        try:
            app = bundle.find_app(args.app)
        except FileNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        res = bundle.remove_app(app, purge=args.purge)
        print(json.dumps(res, indent=2))
        return 0
    print(f"unknown app action {args.action}", file=sys.stderr)
    return 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="porte",
        description="Execution-guided nativeization research harness")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("build", help="build fixture images for both archs")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=_cmd_build)

    p = sub.add_parser("analyze", help="static inventory of a module")
    p.add_argument("binary")
    p.add_argument("--json")
    p.set_defaults(fn=_cmd_analyze)

    p = sub.add_parser("profile", help="run the baseline with the probe on")
    p.add_argument("--arch", default="x86_64", choices=["x86_64", "arm64"])
    p.add_argument("--workload", default="all")
    p.set_defaults(fn=_cmd_profile)

    p = sub.add_parser("specialize", help="generate a guarded ARM64 module")
    p.add_argument("--arch", default="arm64")
    p.add_argument("--out")
    p.set_defaults(fn=_cmd_specialize)

    p = sub.add_parser("verify", help="differential oracle over the corpus")
    p.add_argument("--dylib", required=True)
    p.set_defaults(fn=_cmd_verify)

    p = sub.add_parser("run", help="run one arm and print its digest")
    p.add_argument("--arm", default="spec",
                   choices=["baseline", "native", "spec"])
    p.add_argument("--workload", required=True)
    p.add_argument("--dylib")
    p.add_argument("--ledger")
    p.set_defaults(fn=_cmd_run)

    p = sub.add_parser("bench", help="measure all arms")
    p.add_argument("--dylib", required=True)
    p.add_argument("--workload", action="append")
    p.add_argument("--repeats", type=int, default=7)
    p.set_defaults(fn=_cmd_bench)

    p = sub.add_parser("experiment", help="profile, specialize, verify, cache, bench")
    p.add_argument("--repeats", type=int, default=7)
    p.add_argument("--force-build", action="store_true")
    p.set_defaults(fn=_cmd_experiment)

    p = sub.add_parser("warm", help="reload a cached artifact and re-check it")
    p.add_argument("--key", default="")
    p.set_defaults(fn=_cmd_warm)

    p = sub.add_parser("selftest",
                       help="prove the oracle rejects a known-buggy module")
    p.set_defaults(fn=_cmd_selftest)

    p = sub.add_parser("cache", help="inspect the optimization cache")
    p.add_argument("action", choices=["list", "show"])
    p.add_argument("--key", default="")
    p.set_defaults(fn=_cmd_cache)

    p = sub.add_parser("app", help="generate and manage macOS application bundles")
    app_sub = p.add_subparsers(dest="action", required=True)
    a = app_sub.add_parser("install", help="package an EXE as a .app")
    a.add_argument("exe")
    a.add_argument("--name")
    a.add_argument("--bundle-id")
    a.add_argument("--dest", default=str(Path.home() / "Applications"))
    a.add_argument("--args", nargs="*")
    a.add_argument("--accept", action="store_true",
                   help="record user consent without an interactive prompt")
    a.add_argument("--force", action="store_true")
    a.add_argument("--no-dlls", action="store_true")
    a.add_argument("--runtime", default="auto",
                   choices=["auto", "engine", "wine"],
                   help="which runtime executes the program")
    a.add_argument("--no-siblings", action="store_true",
                   help="copy only DLLs next to the executable, not its "
                        "whole portable folder")
    a.set_defaults(fn=_cmd_app)
    a = app_sub.add_parser("list", help="list installed Porte applications")
    a.set_defaults(fn=_cmd_app)
    a = app_sub.add_parser("inspect", help="show a bundle's recorded identity")
    a.add_argument("app")
    a.set_defaults(fn=_cmd_app)
    a = app_sub.add_parser("launch", help="open an installed application")
    a.add_argument("app")
    a.set_defaults(fn=_cmd_app)
    a = app_sub.add_parser("import",
                           help="install a package (.exe, .msi, .msix, .appx)")
    a.add_argument("package")
    a.add_argument("--name", help="display name for a portable .exe")
    a.add_argument("--dest", default=None,
                   help="directory for generated bundles (default ~/Applications)")
    a.add_argument("--args", nargs="*", help="arguments for a portable .exe")
    a.add_argument("--accept", action="store_true")
    a.add_argument("--force", action="store_true")
    a.add_argument("--no-bundles", action="store_true",
                   help="install files only, do not generate .app bundles")
    a.add_argument("--no-dlls", action="store_true",
                   help="do not copy sibling DLLs for a portable .exe")
    a.add_argument("--allow-custom-actions", action="store_true",
                   help="install an MSI that declares custom actions anyway "
                        "(the actions themselves are still not executed)")
    a.add_argument("--runtime", default="auto",
                   choices=["auto", "engine", "wine"],
                   help="'wine' runs the package through the Windows "
                        "Installer instead of Porte's own importer")
    a.add_argument("--no-siblings", action="store_true",
                   help="copy only DLLs next to a portable executable")
    a.set_defaults(fn=_cmd_app)
    a = app_sub.add_parser("products",
                           help="list installed Windows products")
    a.set_defaults(fn=_cmd_app)
    a = app_sub.add_parser("package-info",
                           help="inspect an MSI/MSIX without installing it")
    a.add_argument("package")
    a.set_defaults(fn=_cmd_app)
    a = app_sub.add_parser("uninstall", help="remove a product and its apps")
    a.add_argument("product")
    a.set_defaults(fn=_cmd_app)
    a = app_sub.add_parser("repair", help="verify and re-extract a product")
    a.add_argument("product")
    a.set_defaults(fn=_cmd_app)
    a = app_sub.add_parser("remove", help="delete a generated application")
    a.add_argument("app")
    a.add_argument("--purge", action="store_true",
                   help="also delete the application's state and cache")
    a.set_defaults(fn=_cmd_app)

    p = sub.add_parser("runtime",
                       help="runtime backends: status, verification, benchmarks")
    rsub = p.add_subparsers(dest="action", required=True)
    r = rsub.add_parser("list", help="which runtimes are available")
    r.set_defaults(fn=_cmd_runtime)
    r = rsub.add_parser("verify", help="check a runtime's dependencies")
    r.add_argument("--backend", default="wine", choices=["engine", "wine"])
    r.add_argument("--path", help="a specific runtime to check")
    r.add_argument("--smoke", action="store_true",
                   help="also execute a guest program")
    r.set_defaults(fn=_cmd_runtime)
    r = rsub.add_parser("verify-e2e",
                        help="install, launch and drive real Windows programs")
    r.add_argument("--only", action="append",
                   choices=["fixture", "notepadpp", "7zip"])
    r.add_argument("--keep", action="store_true",
                   help="keep the generated bundles for inspection")
    r.set_defaults(fn=_cmd_runtime)
    r = rsub.add_parser("bench",
                        help="measure translation, API, startup and memory")
    r.add_argument("--repeats", type=int, default=5)
    r.add_argument("--iterations", type=int, default=20000000)
    r.add_argument("--no-gui", action="store_true")
    r.set_defaults(fn=_cmd_runtime)

    p = sub.add_parser("frame",
                       help="measure the frame-path gap against native ARM64")
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--no-scaling", action="store_true")
    p.set_defaults(fn=_cmd_frame)

    p = sub.add_parser("pe", help="opaque Windows PE pipeline")
    p.add_argument("action", choices=["analyze", "optimize", "verify", "run",
                                      "bench", "selftest", "warm", "experiment"])
    p.add_argument("exe", nargs="?", default="fixtures/opaque/out/blob.exe")
    p.add_argument("--workload", default="65536")
    p.add_argument("--force", action="store_true")
    p.add_argument("--baseline", action="store_true")
    p.add_argument("--repeats", type=int, default=7)
    p.set_defaults(fn=_cmd_pe)

    args = parser.parse_args(argv)
    return args.fn(args)


def legacy_main(argv=None) -> int:
    """Entry point for the deprecated `forge` command."""
    print("porte: `forge` is the previous name of this tool; use `porte`. "
          "The alias will be removed in a future release.", file=sys.stderr)
    return main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
