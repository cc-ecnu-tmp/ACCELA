plugins {
    id("java")
    id("org.graalvm.buildtools.native") version "0.10.3"
}

group = "accela"
version = "1.0-SNAPSHOT"

repositories {
    mavenCentral()
}

dependencies {
    testImplementation(platform("org.junit:junit-bom:5.10.0"))
    testImplementation("org.junit.jupiter:junit-jupiter")
    testRuntimeOnly("org.junit.platform:junit-platform-launcher")
}

tasks.withType<Jar>(){
    manifest{
        attributes["Main-Class"] = "Compiler"
    }
}

tasks.test {
    useJUnitPlatform()
}

val targetProfilePython = providers.gradleProperty("targetProfilePython").orElse("python")
val targetProfileSource = providers.gradleProperty("targetProfile")
    .orElse("config/target/boomv3-development.json")
val generatedTargetProfile = "src/main/java/accela/cost/GeneratedTargetProfile.java"

val verifyTargetProfile by tasks.registering(Exec::class) {
    group = "verification"
    description = "Strictly validates the JSON TargetProfile and verifies its embedded Java source."
    commandLine(targetProfilePython.get(), "-m", "tools.targetlab", "validate", targetProfileSource.get())
    doLast {
        exec {
            commandLine(targetProfilePython.get(), "-m", "tools.targetlab", "verify-embedded",
                targetProfileSource.get(), generatedTargetProfile)
        }
    }
}

tasks.register<Exec>("embedTargetProfile") {
    group = "build setup"
    description = "Validates a JSON TargetProfile and regenerates the embedded Java profile."
    commandLine(targetProfilePython.get(), "-m", "tools.targetlab", "embed",
        targetProfileSource.get(), generatedTargetProfile)
}

tasks.named("check") {
    dependsOn(verifyTargetProfile)
}

val testJavaDir = layout.projectDirectory.dir("src/test/java")

fun renameTestSources(fromSuffix: String, toSuffix: String) {
    testJavaDir.asFile
        .walkTopDown()
        .filter { it.isFile && it.name.endsWith(fromSuffix) }
        .forEach { file ->
            file.renameTo(file.resolveSibling(file.name.removeSuffix(fromSuffix) + toSuffix))
        }
}

val restoreTestSources by tasks.registering {
    doLast {
        renameTestSources(".java", ".testj")
    }
}

val prepareTestSources by tasks.registering {
    doLast {
        renameTestSources(".testj", ".java")
    }
}

tasks.named<JavaCompile>("compileTestJava") {
    dependsOn(prepareTestSources)
    finalizedBy(restoreTestSources)
}

tasks.test {
    finalizedBy(restoreTestSources)
}

graalvmNative {
    binaries {
        named("main") {
            imageName.set("accela")
            mainClass.set("accela.Main")
            buildArgs.add("--no-fallback")
        }
    }
}
