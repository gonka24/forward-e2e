// Out-of-tree Gradle init script.
//
// Used for three builds, all invoked by the runner with
//   -I <runner>/harness/testermint/gradle/out-of-tree.init.gradle.kts
//   -Pa8.outRoot=<absolute dir outside every source snapshot>
//   --project-cache-dir <W>/gradle-project-cache/<build>
//   -Pkotlin.project.persistent.dir=<W>/kotlin/<build>
//   --no-daemon
// 1. upstream Testermint:   --project-dir <gonka>/testermint
//                           (+ -Pa8.classpathFile=<W>/testermint-classpath.txt, task a8ExportClasspath)
// 2. upstream mock server:  --project-dir <gonka>/testermint/mock_server (task shadowJar)
// 3. the Marketplace harness: --project-dir <runner>/harness/testermint
//
// Why: the Gonka checkout is an immutable source snapshot. Gradle and the
// Kotlin plugin would otherwise write build/, .gradle/ and .kotlin/ into it.
// This script moves every project's build directory to
//   root project:  <a8.outRoot>/<rootProject.name>
//   subproject:    <a8.outRoot>/<rootProject.name>/<project path, ':' -> '/'>
// e.g. <outRoot>/testermint, <outRoot>/testermint/mock_server,
// <outRoot>/mock_server (standalone) and <outRoot>/marketplace-testermint-harness.
// .gradle/ and .kotlin/ cannot be moved from an init script (they are created
// before or independently of project evaluation), so the runner passes
// --project-cache-dir and -Pkotlin.project.persistent.dir itself. Anything that
// still escapes into a snapshot is caught by the runner's post-run source
// snapshot comparison (forward_e2e/suite/source_snapshot.py), which fails the run.
//
// This file is part of the runner, never of the Gonka tree.

import groovy.json.JsonOutput
import org.gradle.api.tasks.SourceSetContainer
import java.io.File

fun a8StartProperty(name: String): String? =
    gradle.startParameter.projectProperties[name]?.trim()?.takeIf { it.isNotEmpty() }

fun a8AbsoluteFile(name: String, value: String): File {
    val file = File(value)
    if (!file.isAbsolute) throw GradleException("-P$name must be an absolute path: $value")
    return file.canonicalFile
}

fun a8IsWithin(child: File, parent: File): Boolean {
    val c = child.canonicalFile.toPath()
    val p = parent.canonicalFile.toPath()
    return c == p || c.startsWith(p)
}

val a8OutRoot: File = a8AbsoluteFile(
    "a8.outRoot",
    a8StartProperty("a8.outRoot")
        ?: throw GradleException("-Pa8.outRoot is required by out-of-tree.init.gradle.kts"),
)
val a8ClasspathFile: File? = a8StartProperty("a8.classpathFile")?.let { a8AbsoluteFile("a8.classpathFile", it) }

fun a8BuildDirFor(project: Project): File {
    val rootDir = File(a8OutRoot, a8Segment(project.rootProject.name))
    if (project.path == ":") return rootDir
    val relative = project.path.removePrefix(":").split(':').joinToString("/") { a8Segment(it) }
    return File(rootDir, relative)
}

// Project names become path segments; refuse anything that could escape outRoot.
fun a8Segment(name: String): String {
    if (name.isEmpty() || name == "." || name == ".." || name.contains('/') || name.contains('\\')) {
        throw GradleException("project name cannot be used as an out-of-tree path segment: '$name'")
    }
    return name
}

gradle.beforeProject {
    val target = a8BuildDirFor(this)
    if (a8IsWithin(target, rootProject.projectDir) || a8IsWithin(target, projectDir)) {
        throw GradleException(
            "a8.outRoot must be outside the project tree: build dir $target is inside ${rootProject.projectDir}"
        )
    }
    layout.buildDirectory.set(target)
}

gradle.afterProject {
    // A build script that resets buildDir after us would silently write into
    // the source snapshot again; fail instead.
    val expected = a8BuildDirFor(this)
    val actual = layout.buildDirectory.get().asFile.canonicalFile
    if (actual != expected.canonicalFile) {
        throw GradleException("project $path changed its build directory to $actual (expected $expected)")
    }
}

// Registered only when -Pa8.classpathFile is given, so the mock_server and
// harness builds are unaffected.
a8ClasspathFile?.let { classpathFile ->
    gradle.rootProject {
        val exported = this
        if (a8IsWithin(classpathFile, exported.projectDir)) {
            throw GradleException("-Pa8.classpathFile must be outside the project tree: $classpathFile")
        }
        exported.pluginManager.withPlugin("java") {
            val main = exported.extensions.getByType(SourceSetContainer::class.java).getByName("main")
            val runtime = exported.configurations.getByName("runtimeClasspath")
            exported.tasks.register("a8ExportClasspath") {
                group = "a8"
                description = "Writes upstream main output dirs + runtimeClasspath to -Pa8.classpathFile."
                dependsOn(main.classesTaskName)
                inputs.files(runtime)
                outputs.file(classpathFile)
                outputs.file(File(classpathFile.path + ".json"))
                outputs.upToDateWhen { false }
                doLast {
                    // Main output first, so upstream classes are resolved from
                    // exactly what this build compiled, then upstream's own
                    // resolved runtime dependencies in Gradle's order.
                    val declaredOutput = main.output.classesDirs.files.toList() +
                        listOfNotNull(main.output.resourcesDir)
                    // A source set without Java (or without resources) has no
                    // such directory; listing it would make the consumer fail
                    // on a path that carries nothing.
                    val mainOutput = declaredOutput.filter { it.exists() }.map { it.canonicalFile }
                    if (mainOutput.none { File(it, "com/productscience").isDirectory }) {
                        throw GradleException("upstream main output has no com/productscience classes: $declaredOutput")
                    }
                    val runtimeFiles = runtime.files.map { it.canonicalFile }
                    val entries = LinkedHashSet<File>().apply {
                        addAll(mainOutput)
                        addAll(runtimeFiles)
                    }
                    entries.forEach { entry ->
                        if (entry.path.contains('\n') || entry.path.contains('\r')) {
                            throw GradleException("classpath entry contains a line break: ${entry.path}")
                        }
                    }
                    classpathFile.parentFile.mkdirs()
                    val tmp = File(classpathFile.path + ".tmp")
                    tmp.writeText(entries.joinToString(separator = "\n", postfix = "\n") { it.path }, Charsets.UTF_8)
                    if (!tmp.renameTo(classpathFile)) {
                        classpathFile.delete()
                        if (!tmp.renameTo(classpathFile)) {
                            throw GradleException("cannot write $classpathFile")
                        }
                    }
                    val metadata = linkedMapOf(
                        "schema" to "a8.testermint-classpath/1",
                        "gradle_version" to exported.gradle.gradleVersion,
                        "java_version" to System.getProperty("java.version"),
                        "java_vendor" to System.getProperty("java.vendor"),
                        "root_project_name" to exported.name,
                        "project_path" to exported.path,
                        "project_version" to exported.version.toString(),
                        "project_group" to exported.group.toString(),
                        "project_dir" to exported.projectDir.canonicalPath,
                        "build_dir" to exported.layout.buildDirectory.get().asFile.canonicalPath,
                        "classpath_file" to classpathFile.path,
                        "main_output" to mainOutput.map { it.path },
                        "main_output_missing" to declaredOutput.filterNot { it.exists() }.map { it.path },
                        "runtime_classpath" to runtimeFiles.map { it.path },
                        "entry_count" to entries.size,
                    )
                    File(classpathFile.path + ".json").writeText(
                        JsonOutput.prettyPrint(JsonOutput.toJson(metadata)) + "\n",
                        Charsets.UTF_8,
                    )
                }
            }
        }
    }
}
