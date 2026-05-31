// --- ts_utilities.ts ---
import {
    __String,
    AccessExpression,
    AccessorDeclaration,
    addRange,
    affectsDeclarationPathOptionDeclarations,
    affectsEmitOptionDeclarations,
    AliasDeclarationNode,
    AllAccessorDeclarations,
    AmbientModuleDeclaration,
    AmpersandAmpersandEqualsToken,
    AnyImportOrBareOrAccessedRequire,
    AnyImportOrReExport,
    AnyImportSyntax,
    AnyValidImportOrReExport,
    append,
    arrayFrom,
    ArrayLiteralExpression,
    ArrayTypeNode,
    ArrowFunction,
    AsExpression,
    AssertionExpression,
    assertType,
    AssignmentDeclarationKind,
    AssignmentExpression,
    AssignmentOperatorToken,
    BarBarEqualsToken,
    BinaryExpression,
    binarySearch,
    BindableObjectDefinePropertyCall,
    BindableStaticAccessExpression,
    BindableStaticElementAccessExpression,
    BindableStaticNameExpression,
    BindingElement,
    BindingElementOfBareOrAccessedRequire,
    Block,
    BundleFileSection,
    BundleFileSectionKind,
    BundleFileTextLike,
    CallExpression,
    CallLikeExpression,
    CallSignatureDeclaration,
    canHaveDecorators,
    canHaveModifiers,
    CaseBlock,
    CaseClause,
    CaseOrDefaultClause,
    CatchClause,
    changeAnyExtension,
    CharacterCodes,
    CheckFlags,
    ClassDeclaration,
    ClassElement,
    ClassExpression,
    classHasDeclaredOrExplicitlyAssignedName,
    ClassLikeDeclaration,
    ClassStaticBlockDeclaration,
    combinePaths,
    CommaListExpression,
    CommandLineOption,
    CommentDirective,
    CommentDirectivesMap,
    CommentDirectiveType,
    CommentRange,
    comparePaths,
    compareStringsCaseSensitive,
    compareValues,
    Comparison,
    CompilerOptions,
    ComputedPropertyName,
    computeLineAndCharacterOfPosition,
    computeLineOfPosition,
    computeLineStarts,
    concatenate,
    ConditionalExpression,
    ConstructorDeclaration,
    ConstructSignatureDeclaration,
    ContainerFlags,
    contains,
    containsPath,
    createGetCanonicalFileName,
    createMultiMap,
    createScanner,
    createTextSpan,
    createTextSpanFromBounds,
    Debug,
    Declaration,
    DeclarationName,
    DeclarationWithTypeParameterChildren,
    DeclarationWithTypeParameters,
    Decorator,
    DefaultClause,
    DestructuringAssignment,
    Diagnostic,
    DiagnosticArguments,
    DiagnosticCollection,
    DiagnosticMessage,
    DiagnosticMessageChain,
    DiagnosticRelatedInformation,
    Diagnostics,
    DiagnosticWithDetachedLocation,
    DiagnosticWithLocation,
    directorySeparator,
    DoStatement,
    DynamicNamedBinaryExpression,
    DynamicNamedDeclaration,
    ElementAccessExpression,
    EmitFlags,
    EmitHost,
    EmitResolver,
    EmitTextWriter,
    emptyArray,
    endsWith,
    ensurePathIsNonModuleName,
    ensureTrailingDirectorySeparator,
    EntityName,
    EntityNameExpression,
    EntityNameOrEntityNameExpression,
    EnumDeclaration,
    EqualityComparer,
    equalOwnProperties,
    EqualsToken,
    equateValues,
    escapeLeadingUnderscores,
    every,
    ExportAssignment,
    ExportDeclaration,
    ExportSpecifier,
    Expression,
    ExpressionStatement,
    ExpressionWithTypeArguments,
    Extension,
    ExternalModuleReference,
    factory,
    FileExtensionInfo,
    fileExtensionIs,
    fileExtensionIsOneOf,
    FileWatcher,
    filter,
    find,
    findAncestor,
    findBestPatternMatch,
    findIndex,
    findLast,
    firstDefined,
    firstOrUndefined,
    flatMap,
    flatMapToMutable,
    flatten,
    forEach,
    forEachAncestorDirectory,
    forEachChild,
    forEachChildRecursively,
    ForInOrOfStatement,
    ForStatement,
    FunctionBody,
    FunctionDeclaration,
    FunctionExpression,
    FunctionLikeDeclaration,
    GetAccessorDeclaration,
    getAllJSDocTags,
    getBaseFileName,
    GetCanonicalFileName,
    getCombinedModifierFlags,
    getCombinedNodeFlags,
    getCommonSourceDirectory,
    getContainerFlags,
    getDirectoryPath,
    getJSDocAugmentsTag,
    getJSDocDeprecatedTagNoCache,
    getJSDocImplementsTags,
    getJSDocOverrideTagNoCache,
    getJSDocParameterTags,
    getJSDocParameterTagsNoCache,
    getJSDocPrivateTagNoCache,
    getJSDocProtectedTagNoCache,
    getJSDocPublicTagNoCache,
    getJSDocReadonlyTagNoCache,
    getJSDocReturnType,
    getJSDocSatisfiesTag,
    getJSDocTags,
    getJSDocType,
    getJSDocTypeParameterTags,
    getJSDocTypeParameterTagsNoCache,
    getJSDocTypeTag,
    getLeadingCommentRanges,
    getLineAndCharacterOfPosition,
    getLinesBetweenPositions,
    getLineStarts,
    getModeForUsageLocation,
    getNameOfDeclaration,
    getNormalizedAbsolutePath,
    getNormalizedPathComponents,
    getOwnKeys,
    getParseTreeNode,
    getPathComponents,
    getPathFromPathComponents,
    getRelativePathToDirectoryOrUrl,
    getResolutionModeOverride,
    getRootLength,
    getSnippetElement,
    getStringComparer,
    getSymbolId,
    getTrailingCommentRanges,
    HasExpressionInitializer,
    hasExtension,
    HasFlowNode,
    HasInitializer,
    hasInitializer,
    HasJSDoc,
    hasJSDocNodes,
    HasModifiers,
    hasProperty,
    HasType,
    HasTypeArguments,
    HeritageClause,
    Identifier,
    identifierToKeywordKind,
    IdentifierTypePredicate,
    identity,
    idText,
    IfStatement,
    ignoredPaths,
    ImportAttribute,
    ImportCall,
    ImportClause,
    ImportDeclaration,
    ImportEqualsDeclaration,
    ImportMetaProperty,
    ImportSpecifier,
    ImportTypeNode,
    IndexInfo,
    indexOfAnyCharCode,
    IndexSignatureDeclaration,
    InitializedVariableDeclaration,
    insertSorted,
    InstanceofExpression,
    InterfaceDeclaration,
    InternalEmitFlags,
    isAccessor,
    isAnyDirectorySeparator,
    isArray,
    isArrayLiteralExpression,
    isArrowFunction,
    isAutoAccessorPropertyDeclaration,
    isBigIntLiteral,
    isBinaryExpression,
    isBindingElement,
    isBindingPattern,
    isCallExpression,
    isClassDeclaration,
    isClassElement,
    isClassExpression,
    isClassLike,
    isClassStaticBlockDeclaration,
    isCommaListExpression,
    isComputedPropertyName,
    isConstructorDeclaration,
    isDeclaration,
    isDecorator,
    isElementAccessExpression,
    isEnumDeclaration,
    isEnumMember,
    isExportAssignment,
    isExportDeclaration,
    isExpressionStatement,
    isExpressionWithTypeArguments,
    isExternalModule,
    isExternalModuleReference,
    isFileProbablyExternalModule,
    isForStatement,
    isFunctionDeclaration,
    isFunctionExpression,
    isFunctionLike,
    isFunctionLikeDeclaration,
    isFunctionLikeOrClassStaticBlockDeclaration,
    isGetAccessorDeclaration,
    isHeritageClause,
    isIdentifier,
    isIdentifierStart,
    isIdentifierText,
    isImportTypeNode,
    isInterfaceDeclaration,
    isJSDoc,
    isJSDocAugmentsTag,
    isJSDocFunctionType,
    isJSDocImplementsTag,
    isJSDocLinkLike,
    isJSDocMemberName,
    isJSDocNameReference,
    isJSDocNode,
    isJSDocOverloadTag,
    isJSDocParameterTag,
    isJSDocPropertyLikeTag,
    isJSDocSatisfiesTag,
    isJSDocSignature,
    isJSDocTag,
    isJSDocTemplateTag,
    isJSDocTypeExpression,
    isJSDocTypeLiteral,
    isJSDocTypeTag,
    isJsxChild,
    isJsxFragment,
    isJsxNamespacedName,
    isJsxOpeningLikeElement,
    isJsxText,
    isLeftHandSideExpression,
    isLineBreak,
    isLiteralTypeNode,
    isMemberName,
    isMetaProperty,
    isMethodDeclaration,
    isMethodOrAccessor,
    isModifierLike,
    isModuleDeclaration,
    isNamedDeclaration,
    isNamespaceExport,
    isNamespaceExportDeclaration,
    isNamespaceImport,
    isNonNullExpression,
    isNoSubstitutionTemplateLiteral,
    isNumericLiteral,
    isObjectLiteralExpression,
    isOmittedExpression,
    isParameter,
    isParameterPropertyDeclaration,
    isParenthesizedExpression,
    isParenthesizedTypeNode,
    isPrefixUnaryExpression,
    isPrivateIdentifier,
    isPropertyAccessExpression,
    isPropertyAssignment,
    isPropertyDeclaration,
    isPropertyName,
    isPropertySignature,
    isQualifiedName,
    isRootedDiskPath,
    isSetAccessorDeclaration,
    isShiftOperatorOrHigher,
    isShorthandPropertyAssignment,
    isSourceFile,
    isString,
    isStringLiteral,
    isStringLiteralLike,
    isTypeAliasDeclaration,
    isTypeElement,
    isTypeLiteralNode,
    isTypeNode,
    isTypeParameterDeclaration,
    isTypeReferenceNode,
    isVariableDeclaration,
    isVariableStatement,
    isVoidExpression,
    isWhiteSpaceLike,
    isWhiteSpaceSingleLine,
    JSDoc,
    JSDocArray,
    JSDocCallbackTag,
    JSDocEnumTag,
    JSDocMemberName,
    JSDocOverloadTag,
    JSDocParameterTag,
    JSDocPropertyLikeTag,
    JSDocSatisfiesExpression,
    JSDocSatisfiesTag,
    JSDocSignature,
    JSDocTag,
    JSDocTemplateTag,
    JSDocTypedefTag,
    JsonSourceFile,
    JsxAttributeName,
    JsxChild,
    JsxElement,
    JsxEmit,
    JsxFragment,
    JsxNamespacedName,
    JsxOpeningElement,
    JsxOpeningLikeElement,
    JsxSelfClosingElement,
    JsxTagNameExpression,
    KeywordSyntaxKind,
    LabeledStatement,
    LanguageVariant,
    last,
    lastOrUndefined,
    LateVisibilityPaintedStatement,
    length,
    LiteralImportTypeNode,
    LiteralLikeElementAccessExpression,
    LiteralLikeNode,
    LogicalOperator,
    LogicalOrCoalescingAssignmentOperator,
    mangleScopedPackageName,
    map,
    mapDefined,
    MapLike,
    MemberName,
    memoize,
    MetaProperty,
    MethodDeclaration,
    MethodSignature,
    ModeAwareCache,
    ModifierFlags,
    ModifierLike,
    ModuleBlock,
    ModuleDeclaration,
    ModuleDetectionKind,
    ModuleKind,
    ModuleResolutionKind,
    moduleResolutionOptionDeclarations,
    MultiMap,
    NamedDeclaration,
    NamedExports,
    NamedImports,
    NamedImportsOrExports,
    NamespaceExport,
    NamespaceImport,
    NewExpression,
    NewLineKind,
    Node,
    NodeArray,
    NodeFlags,
    nodeModulesPathPart,
    NonNullExpression,
    noop,
    normalizePath,
    NoSubstitutionTemplateLiteral,
    NumberLiteralType,
    NumericLiteral,
    ObjectFlags,
    ObjectFlagsType,
    ObjectLiteralElement,
    ObjectLiteralExpression,
    ObjectLiteralExpressionBase,
    ObjectTypeDeclaration,
    optionsAffectingProgramStructure,
    or,
    OuterExpressionKinds,
    PackageId,
    ParameterDeclaration,
    ParenthesizedExpression,
    ParenthesizedTypeNode,
    parseConfigFileTextToJson,
    PartiallyEmittedExpression,
    Path,
    pathIsRelative,
    Pattern,
    PostfixUnaryExpression,
    PrefixUnaryExpression,
    PrinterOptions,
    PrintHandlers,
    PrivateIdentifier,
    ProjectReference,
    PrologueDirective,
    PropertyAccessEntityNameExpression,
    PropertyAccessExpression,
    PropertyAssignment,
    PropertyDeclaration,
    PropertyName,
    PropertyNameLiteral,
    PropertySignature,
    PseudoBigInt,
    PunctuationOrKeywordSyntaxKind,
    PunctuationSyntaxKind,
    QualifiedName,
    QuestionQuestionEqualsToken,
    ReadonlyCollection,
    ReadonlyTextRange,
    removeTrailingDirectorySeparator,
    RequireOrImportCall,
    RequireVariableStatement,
    ResolutionMode,
    ResolvedModuleFull,
    ResolvedModuleWithFailedLookupLocations,
    ResolvedTypeReferenceDirective,
    ResolvedTypeReferenceDirectiveWithFailedLookupLocations,
    ReturnStatement,
    SatisfiesExpression,
    ScriptKind,
    ScriptTarget,
    semanticDiagnosticsOptionDeclarations,
    SetAccessorDeclaration,
    ShorthandPropertyAssignment,
    shouldAllowImportingTsExtension,
    Signature,
    SignatureDeclaration,
    SignatureFlags,
    singleElementArray,
    singleOrUndefined,
    skipOuterExpressions,
    skipTrivia,
    SnippetKind,
    some,
    sort,
    SortedArray,
    SourceFile,
    SourceFileLike,
    SourceFileMayBeEmittedHost,
    SourceMapSource,
    startsWith,
    startsWithUseStrict,
    Statement,
    StringLiteral,
    StringLiteralLike,
    StringLiteralType,
    stringToToken,
    SuperCall,
    SuperExpression,
    SuperProperty,
    SwitchStatement,
    Symbol,
    SymbolFlags,
    SymbolTable,
    SyntaxKind,
    SyntaxList,
    TaggedTemplateExpression,
    TemplateLiteral,
    TemplateLiteralLikeNode,
    TemplateLiteralToken,
    TemplateLiteralTypeSpan,
    TemplateSpan,
    TextRange,
    TextSpan,
    ThisTypePredicate,
    Token,
    TokenFlags,
    tokenToString,
    toPath,
    tracing,
    TransformFlags,
    TransientSymbol,
    TriviaSyntaxKind,
    tryCast,
    tryRemovePrefix,
    TryStatement,
    TsConfigSourceFile,
    TupleTypeNode,
    Type,
    TypeAliasDeclaration,
    TypeAssertion,
    TypeChecker,
    TypeCheckerHost,
    TypeElement,
    TypeFlags,
    TypeLiteralNode,
    TypeNode,
    TypeNodeSyntaxKind,
    TypeParameter,
    TypeParameterDeclaration,
    TypePredicate,
    TypePredicateKind,
    TypeReferenceNode,
    unescapeLeadingUnderscores,
    UnionOrIntersectionTypeNode,
    UniqueESSymbolType,
    UserPreferences,
    ValidImportTypeNode,
    VariableDeclaration,
    VariableDeclarationInitializedTo,
    VariableDeclarationList,
    VariableLikeDeclaration,
    VariableStatement,
    WhileStatement,
    WithStatement,
    WrappedExpression,
    WriteFileCallback,
    WriteFileCallbackData,
    YieldExpression,
} from "./_namespaces/ts";

/** @internal */
export const resolvingEmptyArray: never[] = [];

/** @internal */
export const externalHelpersModuleNameText = "tslib";

/** @internal */
export const defaultMaximumTruncationLength = 160;
/** @internal */
export const noTruncationMaximumTruncationLength = 1_000_000;

/** @internal */
export function getDeclarationOfKind<T extends Declaration>(symbol: Symbol, kind: T["kind"]): T | undefined {
    const declarations = symbol.declarations;
    if (declarations) {
        for (const declaration of declarations) {
            if (declaration.kind === kind) {
                return declaration as T;
            }
        }
    }

    return undefined;
}

/** @internal */
export function getDeclarationsOfKind<T extends Declaration>(symbol: Symbol, kind: T["kind"]): T[] {
    return filter(symbol.declarations || emptyArray, d => d.kind === kind) as T[];
}

/** @internal */
export function createSymbolTable(symbols?: readonly Symbol[]): SymbolTable {
    const result = new Map<__String, Symbol>();
    if (symbols) {
        for (const symbol of symbols) {
            result.set(symbol.escapedName, symbol);
        }
    }
    return result;
}

/** @internal */
export function isTransientSymbol(symbol: Symbol): symbol is TransientSymbol {
    return (symbol.flags & SymbolFlags.Transient) !== 0;
}

const stringWriter = createSingleLineStringWriter();

function createSingleLineStringWriter(): EmitTextWriter {
    // Why var? It avoids TDZ checks in the runtime which can be costly.
    // See: https://github.com/microsoft/TypeScript/issues/52924
    /* eslint-disable no-var */
    var str = "";
    /* eslint-enable no-var */
    const writeText: (text: string) => void = text => str += text;
    return {
        getText: () => str,
        write: writeText,
        rawWrite: writeText,
        writeKeyword: writeText,
        writeOperator: writeText,
        writePunctuation: writeText,
        writeSpace: writeText,
        writeStringLiteral: writeText,
        writeLiteral: writeText,
        writeParameter: writeText,
        writeProperty: writeText,
        writeSymbol: (s, _) => writeText(s),
        writeTrailingSemicolon: writeText,
        writeComment: writeText,
        getTextPos: () => str.length,
        getLine: () => 0,
        getColumn: () => 0,
        getIndent: () => 0,
        isAtStartOfLine: () => false,
        hasTrailingComment: () => false,
        hasTrailingWhitespace: () => !!str.length && isWhiteSpaceLike(str.charCodeAt(str.length - 1)),

        // Completely ignore indentation for string writers.  And map newlines to
        // a single space.
        writeLine: () => str += " ",
        increaseIndent: noop,
        decreaseIndent: noop,
        clear: () => str = "",
    };
}

/** @internal */
export function changesAffectModuleResolution(oldOptions: CompilerOptions, newOptions: CompilerOptions): boolean {
    return oldOptions.configFilePath !== newOptions.configFilePath ||
        optionsHaveModuleResolutionChanges(oldOptions, newOptions);
}

/** @internal */
export function optionsHaveModuleResolutionChanges(oldOptions: CompilerOptions, newOptions: CompilerOptions) {
    return optionsHaveChanges(oldOptions, newOptions, moduleResolutionOptionDeclarations);
}

/** @internal */
export function changesAffectingProgramStructure(oldOptions: CompilerOptions, newOptions: CompilerOptions) {
    return optionsHaveChanges(oldOptions, newOptions, optionsAffectingProgramStructure);
}

/** @internal */
export function optionsHaveChanges(oldOptions: CompilerOptions, newOptions: CompilerOptions, optionDeclarations: readonly CommandLineOption[]) {
    return oldOptions !== newOptions && optionDeclarations.some(o => !isJsonEqual(getCompilerOptionValue(oldOptions, o), getCompilerOptionValue(newOptions, o)));
}

/** @internal */
export function forEachAncestor<T>(node: Node, callback: (n: Node) => T | undefined | "quit"): T | undefined {
    while (true) {
        const res = callback(node);
        if (res === "quit") return undefined;
        if (res !== undefined) return res;
        if (isSourceFile(node)) return undefined;
        node = node.parent;
    }
}

/**
 * Calls `callback` for each entry in the map, returning the first truthy result.
 * Use `map.forEach` instead for normal iteration.
 *
 * @internal
 */
export function forEachEntry<K, V, U>(map: ReadonlyMap<K, V>, callback: (value: V, key: K) => U | undefined): U | undefined {
    const iterator = map.entries();
    for (const [key, value] of iterator) {
        const result = callback(value, key);
        if (result) {
            return result;
        }
    }
    return undefined;
}

/**
 * `forEachEntry` for just keys.
 *
 * @internal
 */
export function forEachKey<K, T>(map: ReadonlyCollection<K>, callback: (key: K) => T | undefined): T | undefined {
    const iterator = map.keys();
    for (const key of iterator) {
        const result = callback(key);
        if (result) {
            return result;
        }
    }
    return undefined;
}

/**
 * Copy entries from `source` to `target`.
 *
 * @internal
 */
export function copyEntries<K, V>(source: ReadonlyMap<K, V>, target: Map<K, V>): void {
    source.forEach((value, key) => {
        target.set(key, value);
    });
}

/** @internal */
export function usingSingleLineStringWriter(action: (writer: EmitTextWriter) => void): string {
    const oldString = stringWriter.getText();
    try {
        action(stringWriter);
        return stringWriter.getText();
    }
    finally {
        stringWriter.clear();
        stringWriter.writeKeyword(oldString);
    }
}

/** @internal */
export function getFullWidth(node: Node) {
    return node.end - node.pos;
}

/** @internal */
export function projectReferenceIsEqualTo(oldRef: ProjectReference, newRef: ProjectReference) {
    return oldRef.path === newRef.path &&
        !oldRef.prepend === !newRef.prepend &&
        !oldRef.circular === !newRef.circular;
}

/** @internal */
export function moduleResolutionIsEqualTo(oldResolution: ResolvedModuleWithFailedLookupLocations, newResolution: ResolvedModuleWithFailedLookupLocations): boolean {
    return oldResolution === newResolution ||
        oldResolution.resolvedModule === newResolution.resolvedModule ||
        !!oldResolution.resolvedModule &&
            !!newResolution.resolvedModule &&
            oldResolution.resolvedModule.isExternalLibraryImport === newResolution.resolvedModule.isExternalLibraryImport &&
            oldResolution.resolvedModule.extension === newResolution.resolvedModule.extension &&
            oldResolution.resolvedModule.resolvedFileName === newResolution.resolvedModule.resolvedFileName &&
            oldResolution.resolvedModule.originalPath === newResolution.resolvedModule.originalPath &&
            packageIdIsEqual(oldResolution.resolvedModule.packageId, newResolution.resolvedModule.packageId) &&
            oldResolution.alternateResult === newResolution.alternateResult;
}

/** @internal */
export function createModuleNotFoundChain(sourceFile: SourceFile, host: TypeCheckerHost, moduleReference: string, mode: ResolutionMode, packageName: string) {
    const alternateResult = host.getResolvedModule(sourceFile, moduleReference, mode)?.alternateResult;
    const alternateResultMessage = alternateResult && (getEmitModuleResolutionKind(host.getCompilerOptions()) === ModuleResolutionKind.Node10
        ? [Diagnostics.There_are_types_at_0_but_this_result_could_not_be_resolved_under_your_current_moduleResolution_setting_Consider_updating_to_node16_nodenext_or_bundler, [alternateResult]] as const
        : [
            Diagnostics.There_are_types_at_0_but_this_result_could_not_be_resolved_when_respecting_package_json_exports_The_1_library_may_need_to_update_its_package_json_or_typings,
            [alternateResult, alternateResult.includes(nodeModulesPathPart + "@types/") ? `@types/${mangleScopedPackageName(packageName)}` : packageName],
        ] as const);
    const result = alternateResultMessage
        ? chainDiagnosticMessages(
            /*details*/ undefined,
            alternateResultMessage[0],
            ...alternateResultMessage[1],
        )
        : host.typesPackageExists(packageName)
        ? chainDiagnosticMessages(
            /*details*/ undefined,
            Diagnostics.If_the_0_package_actually_exposes_this_module_consider_sending_a_pull_request_to_amend_https_Colon_Slash_Slashgithub_com_SlashDefinitelyTyped_SlashDefinitelyTyped_Slashtree_Slashmaster_Slashtypes_Slash_1,
            packageName,
            mangleScopedPackageName(packageName),
        )
        : host.packageBundlesTypes(packageName)
        ? chainDiagnosticMessages(
            /*details*/ undefined,
            Diagnostics.If_the_0_package_actually_exposes_this_module_try_adding_a_new_declaration_d_ts_file_containing_declare_module_1,
            packageName,
            moduleReference,
        )
        : chainDiagnosticMessages(
            /*details*/ undefined,
            Diagnostics.Try_npm_i_save_dev_types_Slash_1_if_it_exists_or_add_a_new_declaration_d_ts_file_containing_declare_module_0,
            moduleReference,
            mangleScopedPackageName(packageName),
        );
    if (result) result.repopulateInfo = () => ({ moduleReference, mode, packageName: packageName === moduleReference ? undefined : packageName });
    return result;
}

function packageIdIsEqual(a: PackageId | undefined, b: PackageId | undefined): boolean {
    return a === b || !!a && !!b && a.name === b.name && a.subModuleName === b.subModuleName && a.version === b.version;
}

/** @internal */
export function packageIdToPackageName({ name, subModuleName }: PackageId): string {
    return subModuleName ? `${name}/${subModuleName}` : name;
}

/** @internal */
export function packageIdToString(packageId: PackageId): string {
    return `${packageIdToPackageName(packageId)}@${packageId.version}`;
}

/** @internal */
export function typeDirectiveIsEqualTo(oldResolution: ResolvedTypeReferenceDirectiveWithFailedLookupLocations, newResolution: ResolvedTypeReferenceDirectiveWithFailedLookupLocations): boolean {
    return oldResolution === newResolution ||
        oldResolution.resolvedTypeReferenceDirective === newResolution.resolvedTypeReferenceDirective ||
        !!oldResolution.resolvedTypeReferenceDirective &&
            !!newResolution.resolvedTypeReferenceDirective &&
            oldResolution.resolvedTypeReferenceDirective.resolvedFileName === newResolution.resolvedTypeReferenceDirective.resolvedFileName &&
            !!oldResolution.resolvedTypeReferenceDirective.primary === !!newResolution.resolvedTypeReferenceDirective.primary &&
            oldResolution.resolvedTypeReferenceDirective.originalPath === newResolution.resolvedTypeReferenceDirective.originalPath;
}

/** @internal */
export function hasChangesInResolutions<K, V>(
    names: readonly K[],
    newResolutions: readonly V[],
    getOldResolution: (name: K) => V | undefined,
    comparer: (oldResolution: V, newResolution: V) => boolean,
): boolean {
    Debug.assert(names.length === newResolutions.length);

    for (let i = 0; i < names.length; i++) {
        const newResolution = newResolutions[i];
        const entry = names[i];
        const oldResolution = getOldResolution(entry);
        const changed = oldResolution
            ? !newResolution || !comparer(oldResolution, newResolution)
            : newResolution;
        if (changed) {
            return true;
        }
    }
    return false;
}

// Returns true if this node contains a parse error anywhere underneath it.
/** @internal */
export function containsParseError(node: Node): boolean {
    aggregateChildData(node);
    return (node.flags & NodeFlags.ThisNodeOrAnySubNodesHasError) !== 0;
}

function aggregateChildData(node: Node): void {
    if (!(node.flags & NodeFlags.HasAggregatedChildData)) {
        // A node is considered to contain a parse error if:
        //  a) the parser explicitly marked that it had an error
        //  b) any of it's children reported that it had an error.
        const thisNodeOrAnySubNodesHasError = ((node.flags & NodeFlags.ThisNodeHasError) !== 0) ||
            forEachChild(node, containsParseError);

        // If so, mark ourselves accordingly.
        if (thisNodeOrAnySubNodesHasError) {
            (node as Mutable<Node>).flags |= NodeFlags.ThisNodeOrAnySubNodesHasError;
        }

        // Also mark that we've propagated the child information to this node.  This way we can
        // always consult the bit directly on this node without needing to check its children
        // again.
        (node as Mutable<Node>).flags |= NodeFlags.HasAggregatedChildData;
    }
}

/** @internal */
export function getSourceFileOfNode(node: Node): SourceFile;
/** @internal */
export function getSourceFileOfNode(node: Node | undefined): SourceFile | undefined;
/** @internal */
export function getSourceFileOfNode(node: Node | undefined): SourceFile | undefined {
    while (node && node.kind !== SyntaxKind.SourceFile) {
        node = node.parent;
    }
    return node as SourceFile;
}

/** @internal */
export function getSourceFileOfModule(module: Symbol) {
    return getSourceFileOfNode(module.valueDeclaration || getNonAugmentationDeclaration(module));
}

/** @internal */
export function isPlainJsFile(file: SourceFile | undefined, checkJs: boolean | undefined): boolean {
    return !!file && (file.scriptKind === ScriptKind.JS || file.scriptKind === ScriptKind.JSX) && !file.checkJsDirective && checkJs === undefined;
}

/** @internal */
export function isStatementWithLocals(node: Node) {
    switch (node.kind) {
        case SyntaxKind.Block:
        case SyntaxKind.CaseBlock:
        case SyntaxKind.ForStatement:
        case SyntaxKind.ForInStatement:
        case SyntaxKind.ForOfStatement:
            return true;
    }
    return false;
}

/** @internal */
export function getStartPositionOfLine(line: number, sourceFile: SourceFileLike): number {
    Debug.assert(line >= 0);
    return getLineStarts(sourceFile)[line];
}

// This is a useful function for debugging purposes.
/** @internal */
export function nodePosToString(node: Node): string {
    const file = getSourceFileOfNode(node);
    const loc = getLineAndCharacterOfPosition(file, node.pos);
    return `${file.fileName}(${loc.line + 1},${loc.character + 1})`;
}

/** @internal */
export function getEndLinePosition(line: number, sourceFile: SourceFileLike): number {
    Debug.assert(line >= 0);
    const lineStarts = getLineStarts(sourceFile);

    const lineIndex = line;
    const sourceText = sourceFile.text;
    if (lineIndex + 1 === lineStarts.length) {
        // last line - return EOF
        return sourceText.length - 1;
    }
    else {
        // current line start
        const start = lineStarts[lineIndex];
        // take the start position of the next line - 1 = it should be some line break
        let pos = lineStarts[lineIndex + 1] - 1;
        Debug.assert(isLineBreak(sourceText.charCodeAt(pos)));
        // walk backwards skipping line breaks, stop the the beginning of current line.
        // i.e:
        // <some text>
        // $ <- end of line for this position should match the start position
        while (start <= pos && isLineBreak(sourceText.charCodeAt(pos))) {
            pos--;
        }
        return pos;
    }
}

/**
 * Returns a value indicating whether a name is unique globally or within the current file.
 * Note: This does not consider whether a name appears as a free identifier or not, so at the expression `x.y` this includes both `x` and `y`.
 *
 * @internal
 */
export function isFileLevelUniqueName(sourceFile: SourceFile, name: string, hasGlobalName?: PrintHandlers["hasGlobalName"]): boolean {
    return !(hasGlobalName && hasGlobalName(name)) && !sourceFile.identifiers.has(name);
}

// Returns true if this node is missing from the actual source code. A 'missing' node is different
// from 'undefined/defined'. When a node is undefined (which can happen for optional nodes
// in the tree), it is definitely missing. However, a node may be defined, but still be
// missing.  This happens whenever the parser knows it needs to parse something, but can't
// get anything in the source code that it expects at that location. For example:
//
//          let a: ;
//
// Here, the Type in the Type-Annotation is not-optional (as there is a colon in the source
// code). So the parser will attempt to parse out a type, and will create an actual node.
// However, this node will be 'missing' in the sense that no actual source-code/tokens are
// contained within it.
/** @internal */
export function nodeIsMissing(node: Node | undefined): boolean {
    if (node === undefined) {
        return true;
    }

    return node.pos === node.end && node.pos >= 0 && node.kind !== SyntaxKind.EndOfFileToken;
}

/** @internal */
export function nodeIsPresent(node: Node | undefined): boolean {
    return !nodeIsMissing(node);
}

/**
 * Tests whether `child` is a grammar error on `parent`.
 * @internal
 */
export function isGrammarError(parent: Node, child: Node | NodeArray<Node>) {
    if (isTypeParameterDeclaration(parent)) return child === parent.expression;
    if (isClassStaticBlockDeclaration(parent)) return child === parent.modifiers;
    if (isPropertySignature(parent)) return child === parent.initializer;
    if (isPropertyDeclaration(parent)) return child === parent.questionToken && isAutoAccessorPropertyDeclaration(parent);
    if (isPropertyAssignment(parent)) return child === parent.modifiers || child === parent.questionToken || child === parent.exclamationToken || isGrammarErrorElement(parent.modifiers, child, isModifierLike);
    if (isShorthandPropertyAssignment(parent)) return child === parent.equalsToken || child === parent.modifiers || child === parent.questionToken || child === parent.exclamationToken || isGrammarErrorElement(parent.modifiers, child, isModifierLike);
    if (isMethodDeclaration(parent)) return child === parent.exclamationToken;
    if (isConstructorDeclaration(parent)) return child === parent.typeParameters || child === parent.type || isGrammarErrorElement(parent.typeParameters, child, isTypeParameterDeclaration);
    if (isGetAccessorDeclaration(parent)) return child === parent.typeParameters || isGrammarErrorElement(parent.typeParameters, child, isTypeParameterDeclaration);
    if (isSetAccessorDeclaration(parent)) return child === parent.typeParameters || child === parent.type || isGrammarErrorElement(parent.typeParameters, child, isTypeParameterDeclaration);
    if (isNamespaceExportDeclaration(parent)) return child === parent.modifiers || isGrammarErrorElement(parent.modifiers, child, isModifierLike);
    return false;
}

function isGrammarErrorElement<T extends Node>(nodeArray: NodeArray<T> | undefined, child: Node | NodeArray<Node>, isElement: (node: Node) => node is T) {
    if (!nodeArray || isArray(child) || !isElement(child)) return false;
    return contains(nodeArray, child);
}

function insertStatementsAfterPrologue<T extends Statement>(to: T[], from: readonly T[] | undefined, isPrologueDirective: (node: Node) => boolean): T[] {
    if (from === undefined || from.length === 0) return to;
    let statementIndex = 0;
    // skip all prologue directives to insert at the correct position
    for (; statementIndex < to.length; ++statementIndex) {
        if (!isPrologueDirective(to[statementIndex])) {
            break;
        }
    }
    to.splice(statementIndex, 0, ...from);
    return to;
}

function insertStatementAfterPrologue<T extends Statement>(to: T[], statement: T | undefined, isPrologueDirective: (node: Node) => boolean): T[] {
    if (statement === undefined) return to;
    let statementIndex = 0;
    // skip all prologue directives to insert at the correct position
    for (; statementIndex < to.length; ++statementIndex) {
        if (!isPrologueDirective(to[statementIndex])) {
            break;
        }
    }
    to.splice(statementIndex, 0, statement);
    return to;
}

function isAnyPrologueDirective(node: Node) {
    return isPrologueDirective(node) || !!(getEmitFlags(node) & EmitFlags.CustomPrologue);
}

/**
 * Prepends statements to an array while taking care of prologue directives.
 *
 * @internal
 */
export function insertStatementsAfterStandardPrologue<T extends Statement>(to: T[], from: readonly T[] | undefined): T[] {
    return insertStatementsAfterPrologue(to, from, isPrologueDirective);
}

/** @internal */
export function insertStatementsAfterCustomPrologue<T extends Statement>(to: T[], from: readonly T[] | undefined): T[] {
    return insertStatementsAfterPrologue(to, from, isAnyPrologueDirective);
}

/**
 * Prepends statements to an array while taking care of prologue directives.
 *
 * @internal
 */
export function insertStatementAfterStandardPrologue<T extends Statement>(to: T[], statement: T | undefined): T[] {
    return insertStatementAfterPrologue(to, statement, isPrologueDirective);
}

/** @internal */
export function insertStatementAfterCustomPrologue<T extends Statement>(to: T[], statement: T | undefined): T[] {
    return insertStatementAfterPrologue(to, statement, isAnyPrologueDirective);
}

/**
 * Determine if the given comment is a triple-slash
 *
 * @return true if the comment is a triple-slash comment else false
 *
 * @internal
 */
export function isRecognizedTripleSlashComment(text: string, commentPos: number, commentEnd: number) {
    // Verify this is /// comment, but do the regexp match only when we first can find /// in the comment text
    // so that we don't end up computing comment string and doing match for all // comments
    if (
        text.charCodeAt(commentPos + 1) === CharacterCodes.slash &&
        commentPos + 2 < commentEnd &&
        text.charCodeAt(commentPos + 2) === CharacterCodes.slash
    ) {
        const textSubStr = text.substring(commentPos, commentEnd);
        return fullTripleSlashReferencePathRegEx.test(textSubStr) ||
                fullTripleSlashAMDReferencePathRegEx.test(textSubStr) ||
                fullTripleSlashAMDModuleRegEx.test(textSubStr) ||
                fullTripleSlashReferenceTypeReferenceDirectiveRegEx.test(textSubStr) ||
                fullTripleSlashLibReferenceRegEx.test(textSubStr) ||
                defaultLibReferenceRegEx.test(textSubStr) ?
            true : false;
    }
    return false;
}

/** @internal */
export function isPinnedComment(text: string, start: number) {
    return text.charCodeAt(start + 1) === CharacterCodes.asterisk &&
        text.charCodeAt(start + 2) === CharacterCodes.exclamation;
}

/** @internal */
export function createCommentDirectivesMap(sourceFile: SourceFile, commentDirectives: CommentDirective[]): CommentDirectivesMap {
    const directivesByLine = new Map(
        commentDirectives.map(commentDirective => [
            `${getLineAndCharacterOfPosition(sourceFile, commentDirective.range.end).line}`,
            commentDirective,
        ]),
    );

    const usedLines = new Map<string, boolean>();

    return { getUnusedExpectations, markUsed };

    function getUnusedExpectations() {
        return arrayFrom(directivesByLine.entries())
            .filter(([line, directive]) => directive.type === CommentDirectiveType.ExpectError && !usedLines.get(line))
            .map(([_, directive]) => directive);
    }

    function markUsed(line: number) {
        if (!directivesByLine.has(`${line}`)) {
            return false;
        }

        usedLines.set(`${line}`, true);
        return true;
    }
}

/** @internal */
export function getTokenPosOfNode(node: Node, sourceFile?: SourceFileLike, includeJsDoc?: boolean): number {
    // With nodes that have no width (i.e. 'Missing' nodes), we actually *don't*
    // want to skip trivia because this will launch us forward to the next token.
    if (nodeIsMissing(node)) {
        return node.pos;
    }

    if (isJSDocNode(node) || node.kind === SyntaxKind.JsxText) {
        // JsxText cannot actually contain comments, even though the scanner will think it sees comments
        return skipTrivia((sourceFile || getSourceFileOfNode(node)).text, node.pos, /*stopAfterLineBreak*/ false, /*stopAtComments*/ true);
    }

    if (includeJsDoc && hasJSDocNodes(node)) {
        return getTokenPosOfNode(node.jsDoc![0], sourceFile);
    }

    // For a syntax list, it is possible that one of its children has JSDocComment nodes, while
    // the syntax list itself considers them as normal trivia. Therefore if we simply skip
    // trivia for the list, we may have skipped the JSDocComment as well. So we should process its
    // first child to determine the actual position of its first token.
    if (node.kind === SyntaxKind.SyntaxList && (node as SyntaxList)._children.length > 0) {
        return getTokenPosOfNode((node as SyntaxList)._children[0], sourceFile, includeJsDoc);
    }

    return skipTrivia(
        (sourceFile || getSourceFileOfNode(node)).text,
        node.pos,
        /*stopAfterLineBreak*/ false,
        /*stopAtComments*/ false,
        isInJSDoc(node),
    );
}

/** @internal */
export function getNonDecoratorTokenPosOfNode(node: Node, sourceFile?: SourceFileLike): number {
    const lastDecorator = !nodeIsMissing(node) && canHaveModifiers(node) ? findLast(node.modifiers, isDecorator) : undefined;
    if (!lastDecorator) {
        return getTokenPosOfNode(node, sourceFile);
    }

    return skipTrivia((sourceFile || getSourceFileOfNode(node)).text, lastDecorator.end);
}

/** @internal */
export function getSourceTextOfNodeFromSourceFile(sourceFile: SourceFile, node: Node, includeTrivia = false): string {
    return getTextOfNodeFromSourceText(sourceFile.text, node, includeTrivia);
}

function isJSDocTypeExpressionOrChild(node: Node): boolean {
    return !!findAncestor(node, isJSDocTypeExpression);
}

/** @internal */
export function isExportNamespaceAsDefaultDeclaration(node: Node): boolean {
    return !!(isExportDeclaration(node) && node.exportClause && isNamespaceExport(node.exportClause) && node.exportClause.name.escapedText === "default");
}

/** @internal */
export function getTextOfNodeFromSourceText(sourceText: string, node: Node, includeTrivia = false): string {
    if (nodeIsMissing(node)) {
        return "";
    }

    let text = sourceText.substring(includeTrivia ? node.pos : skipTrivia(sourceText, node.pos), node.end);

    if (isJSDocTypeExpressionOrChild(node)) {
        // strip space + asterisk at line start
        text = text.split(/\r\n|\n|\r/).map(line => line.replace(/^\s*\*/, "").trimStart()).join("\n");
    }

    return text;
}

/** @internal */
export function getTextOfNode(node: Node, includeTrivia = false): string {
    return getSourceTextOfNodeFromSourceFile(getSourceFileOfNode(node), node, includeTrivia);
}

function getPos(range: Node) {
    return range.pos;
}

/**
 * Note: it is expected that the `nodeArray` and the `node` are within the same file.
 * For example, searching for a `SourceFile` in a `SourceFile[]` wouldn't work.
 *
 * @internal
 */
export function indexOfNode(nodeArray: readonly Node[], node: Node) {
    return binarySearch(nodeArray, node, getPos, compareValues);
}

/**
 * Gets flags that control emit behavior of a node.
 *
 * @internal
 */
export function getEmitFlags(node: Node): EmitFlags {
    const emitNode = node.emitNode;
    return emitNode && emitNode.flags || 0;
}

/**
 * Gets flags that control emit behavior of a node.
 *
 * @internal
 */
export function getInternalEmitFlags(node: Node): InternalEmitFlags {
    const emitNode = node.emitNode;
    return emitNode && emitNode.internalFlags || 0;
}

// Map from a type name, to a map of targets to array of features introduced to the type at that target.
/** @internal */
export type ScriptTargetFeatures = ReadonlyMap<string, ReadonlyMap<string, string[]>>;

/** @internal */
export const getScriptTargetFeatures = /* @__PURE__ */ memoize((): ScriptTargetFeatures =>
    new Map(Object.entries({
        Array: new Map(Object.entries({
            es2015: [
                "find",
                "findIndex",
                "fill",
                "copyWithin",
                "entries",
                "keys",
                "values",
            ],
            es2016: [
                "includes",
            ],
            es2019: [
                "flat",
                "flatMap",
            ],
            es2022: [
                "at",
            ],
            es2023: [
                "findLastIndex",
                "findLast",
            ],
        })),
        Iterator: new Map(Object.entries({
            es2015: emptyArray,
        })),
        AsyncIterator: new Map(Object.entries({
            es2015: emptyArray,
        })),
        Atomics: new Map(Object.entries({
            es2017: emptyArray,
        })),
        SharedArrayBuffer: new Map(Object.entries({
            es2017: emptyArray,
        })),
        AsyncIterable: new Map(Object.entries({
            es2018: emptyArray,
        })),
        AsyncIterableIterator: new Map(Object.entries({
            es2018: emptyArray,
        })),
        AsyncGenerator: new Map(Object.entries({
            es2018: emptyArray,
        })),
        AsyncGeneratorFunction: new Map(Object.entries({
            es2018: emptyArray,
        })),
        RegExp: new Map(Object.entries({
            es2015: [
                "flags",
                "sticky",
                "unicode",
            ],
            es2018: [
                "dotAll",
            ],
        })),
        Reflect: new Map(Object.entries({
            es2015: [
                "apply",
                "construct",
                "defineProperty",
                "deleteProperty",
                "get",
                "getOwnPropertyDescriptor",
                "getPrototypeOf",
                "has",
                "isExtensible",
                "ownKeys",
                "preventExtensions",
                "set",
                "setPrototypeOf",
            ],
        })),
        ArrayConstructor: new Map(Object.entries({
            es2015: [
                "from",
                "of",
            ],
        })),
        ObjectConstructor: new Map(Object.entries({
            es2015: [
                "assign",
                "getOwnPropertySymbols",
                "keys",
                "is",
                "setPrototypeOf",
            ],
            es2017: [
                "values",
                "entries",
                "getOwnPropertyDescriptors",
            ],
            es2019: [
                "fromEntries",
            ],
            es2022: [
                "hasOwn",
            ],
        })),
        NumberConstructor: new Map(Object.entries({
            es2015: [
                "isFinite",
                "isInteger",
                "isNaN",
                "isSafeInteger",
                "parseFloat",
                "parseInt",
            ],
        })),
        Math: new Map(Object.entries({
            es2015: [
                "clz32",
                "imul",
                "sign",
                "log10",
                "log2",
                "log1p",
                "expm1",
                "cosh",
                "sinh",
                "tanh",
                "acosh",
                "asinh",
                "atanh",
                "hypot",
                "trunc",
                "fround",
                "cbrt",
            ],
        })),
        Map: new Map(Object.entries({
            es2015: [
                "entries",
                "keys",
                "values",
            ],
        })),
        Set: new Map(Object.entries({
            es2015: [
                "entries",
                "keys",
                "values",
            ],
        })),
        PromiseConstructor: new Map(Object.entries({
            es2015: [
                "all",
                "race",
                "reject",
                "resolve",
            ],
            es2020: [
                "allSettled",
            ],
            es2021: [
                "any",
            ],
        })),
        Symbol: new Map(Object.entries({
            es2015: [
                "for",
                "keyFor",
            ],
            es2019: [
                "description",
            ],
        })),
        WeakMap: new Map(Object.entries({
            es2015: [
                "entries",
                "keys",
                "values",
            ],
        })),
        WeakSet: new Map(Object.entries({
            es2015: [
                "entries",
                "keys",
                "values",
            ],
        })),
        String: new Map(Object.entries({
            es2015: [
                "codePointAt",
                "includes",
                "endsWith",
                "normalize",
                "repeat",
                "startsWith",
                "anchor",
                "big",
                "blink",
                "bold",
                "fixed",
                "fontcolor",
                "fontsize",
                "italics",
                "link",
                "small",
                "strike",
                "sub",
                "sup",
            ],
            es2017: [
                "padStart",
                "padEnd",
            ],
            es2019: [
                "trimStart",
                "trimEnd",
                "trimLeft",
                "trimRight",
            ],
            es2020: [
                "matchAll",
            ],
            es2021: [
                "replaceAll",
            ],
            es2022: [
                "at",
            ],
        })),
        StringConstructor: new Map(Object.entries({
            es2015: [
                "fromCodePoint",
                "raw",
            ],
        })),
        DateTimeFormat: new Map(Object.entries({
            es2017: [
                "formatToParts",
            ],
        })),
        Promise: new Map(Object.entries({
            es2015: emptyArray,
            es2018: [
                "finally",
            ],
        })),
        RegExpMatchArray: new Map(Object.entries({
            es2018: [
                "groups",
            ],
        })),
        RegExpExecArray: new Map(Object.entries({
            es2018: [
                "groups",
            ],
        })),
        Intl: new Map(Object.entries({
            es2018: [
                "PluralRules",
            ],
        })),
        NumberFormat: new Map(Object.entries({
            es2018: [
                "formatToParts",
            ],
        })),
        SymbolConstructor: new Map(Object.entries({
            es2020: [
                "matchAll",
            ],
        })),
        DataView: new Map(Object.entries({
            es2020: [
                "setBigInt64",
                "setBigUint64",
                "getBigInt64",
                "getBigUint64",
            ],
        })),
        BigInt: new Map(Object.entries({
            es2020: emptyArray,
        })),
        RelativeTimeFormat: new Map(Object.entries({
            es2020: [
                "format",
                "formatToParts",
                "resolvedOptions",
            ],
        })),
        Int8Array: new Map(Object.entries({
            es2022: [
                "at",
            ],
            es2023: [
                "findLastIndex",
                "findLast",
            ],
        })),
        Uint8Array: new Map(Object.entries({
            es2022: [
                "at",
            ],
            es2023: [
                "findLastIndex",
                "findLast",
            ],
        })),
        Uint8ClampedArray: new Map(Object.entries({
            es2022: [
                "at",
            ],
            es2023: [
                "findLastIndex",
                "findLast",
            ],
        })),
        Int16Array: new Map(Object.entries({
            es2022: [
                "at",
            ],
            es2023: [
                "findLastIndex",
                "findLast",
            ],
        })),
        Uint16Array: new Map(Object.entries({
            es2022: [
                "at",
            ],
            es2023: [
                "findLastIndex",
                "findLast",
            ],
        })),
        Int32Array: new Map(Object.entries({
            es2022: [
                "at",
            ],
            es2023: [
                "findLastIndex",
                "findLast",
            ],
        })),
        Uint32Array: new Map(Object.entries({
            es2022: [
                "at",
            ],
            es2023: [
                "findLastIndex",
                "findLast",
            ],
        })),
        Float32Array: new Map(Object.entries({
            es2022: [
                "at",
            ],
            es2023: [
                "findLastIndex",
                "findLast",
            ],
        })),
        Float64Array: new Map(Object.entries({
            es2022: [
                "at",
            ],
            es2023: [
                "findLastIndex",
                "findLast",
            ],
        })),
        BigInt64Array: new Map(Object.entries({
            es2020: emptyArray,
            es2022: [
                "at",
            ],
            es2023: [
                "findLastIndex",
                "findLast",
            ],
        })),
        BigUint64Array: new Map(Object.entries({
            es2020: emptyArray,
            es2022: [
                "at",
            ],
            es2023: [
                "findLastIndex",
                "findLast",
            ],
        })),
        Error: new Map(Object.entries({
            es2022: [
                "cause",
            ],
        })),
    }))
);

/** @internal */
export const enum GetLiteralTextFlags {
    None = 0,
    NeverAsciiEscape = 1 << 0,
    JsxAttributeEscape = 1 << 1,
    TerminateUnterminatedLiterals = 1 << 2,
    AllowNumericSeparator = 1 << 3,
}

/** @internal */
export function getLiteralText(node: LiteralLikeNode, sourceFile: SourceFile | undefined, flags: GetLiteralTextFlags) {
    // If we don't need to downlevel and we can reach the original source text using
    // the node's parent reference, then simply get the text as it was originally written.
    if (sourceFile && canUseOriginalText(node, flags)) {
        return getSourceTextOfNodeFromSourceFile(sourceFile, node);
    }

    // If we can't reach the original source text, use the canonical form if it's a number,
    // or a (possibly escaped) quoted form of the original text if it's string-like.
    switch (node.kind) {
        case SyntaxKind.StringLiteral: {
            const escapeText = flags & GetLiteralTextFlags.JsxAttributeEscape ? escapeJsxAttributeString :
                flags & GetLiteralTextFlags.NeverAsciiEscape || (getEmitFlags(node) & EmitFlags.NoAsciiEscaping) ? escapeString :
                escapeNonAsciiString;
            if ((node as StringLiteral).singleQuote) {
                return "'" + escapeText(node.text, CharacterCodes.singleQuote) + "'";
            }
            else {
                return '"' + escapeText(node.text, CharacterCodes.doubleQuote) + '"';
            }
        }
        case SyntaxKind.NoSubstitutionTemplateLiteral:
        case SyntaxKind.TemplateHead:
        case SyntaxKind.TemplateMiddle:
        case SyntaxKind.TemplateTail: {
            // If a NoSubstitutionTemplateLiteral appears to have a substitution in it, the original text
            // had to include a backslash: `not \${a} substitution`.
            const escapeText = flags & GetLiteralTextFlags.NeverAsciiEscape || (getEmitFlags(node) & EmitFlags.NoAsciiEscaping) ? escapeString :
                escapeNonAsciiString;

            const rawText = (node as TemplateLiteralLikeNode).rawText ?? escapeTemplateSubstitution(escapeText(node.text, CharacterCodes.backtick));
            switch (node.kind) {
                case SyntaxKind.NoSubstitutionTemplateLiteral:
                    return "`" + rawText + "`";
                case SyntaxKind.TemplateHead:
                    return "`" + rawText + "${";
                case SyntaxKind.TemplateMiddle:
                    return "}" + rawText + "${";
                case SyntaxKind.TemplateTail:
                    return "}" + rawText + "`";
            }
            break;
        }
        case SyntaxKind.NumericLiteral:
        case SyntaxKind.BigIntLiteral:
            return node.text;
        case SyntaxKind.RegularExpressionLiteral:
            if (flags & GetLiteralTextFlags.TerminateUnterminatedLiterals && node.isUnterminated) {
                return node.text + (node.text.charCodeAt(node.text.length - 1) === CharacterCodes.backslash ? " /" : "/");
            }
            return node.text;
    }

    return Debug.fail(`Literal kind '${node.kind}' not accounted for.`);
}

function canUseOriginalText(node: LiteralLikeNode, flags: GetLiteralTextFlags): boolean {
    if (nodeIsSynthesized(node) || !node.parent || (flags & GetLiteralTextFlags.TerminateUnterminatedLiterals && node.isUnterminated)) {
        return false;
    }

    if (isNumericLiteral(node)) {
        if (node.numericLiteralFlags & TokenFlags.IsInvalid) {
            return false;
        }
        if (node.numericLiteralFlags & TokenFlags.ContainsSeparator) {
            return !!(flags & GetLiteralTextFlags.AllowNumericSeparator);
        }
    }

    return !isBigIntLiteral(node);
}

/** @internal */
export function getTextOfConstantValue(value: string | number) {
    return isString(value) ? '"' + escapeNonAsciiString(value) + '"' : "" + value;
}

// Make an identifier from an external module name by extracting the string after the last "/" and replacing
// all non-alphanumeric characters with underscores
/** @internal */
export function makeIdentifierFromModuleName(moduleName: string): string {
    return getBaseFileName(moduleName).replace(/^(\d)/, "_$1").replace(/\W/g, "_");
}

/** @internal */
export function isBlockOrCatchScoped(declaration: Declaration) {
    return (getCombinedNodeFlags(declaration) & NodeFlags.BlockScoped) !== 0 ||
        isCatchClauseVariableDeclarationOrBindingElement(declaration);
}

/** @internal */
export function isCatchClauseVariableDeclarationOrBindingElement(declaration: Declaration) {
    const node = getRootDeclaration(declaration);
    return node.kind === SyntaxKind.VariableDeclaration && node.parent.kind === SyntaxKind.CatchClause;
}

/** @internal */
export function isAmbientModule(node: Node): node is AmbientModuleDeclaration {
    return isModuleDeclaration(node) && (node.name.kind === SyntaxKind.StringLiteral || isGlobalScopeAugmentation(node));
}

/** @internal */
export function isModuleWithStringLiteralName(node: Node): node is ModuleDeclaration {
    return isModuleDeclaration(node) && node.name.kind === SyntaxKind.StringLiteral;
}

/** @internal */
export function isNonGlobalAmbientModule(node: Node): node is ModuleDeclaration & { name: StringLiteral; } {
    return isModuleDeclaration(node) && isStringLiteral(node.name);
}

/**
 * An effective module (namespace) declaration is either
 * 1. An actual declaration: namespace X { ... }
 * 2. A Javascript declaration, which is:
 *    An identifier in a nested property access expression: Y in `X.Y.Z = { ... }`
 *
 * @internal
 */
export function isEffectiveModuleDeclaration(node: Node) {
    return isModuleDeclaration(node) || isIdentifier(node);
}

/**
 * Given a symbol for a module, checks that it is a shorthand ambient module.
 *
 * @internal
 */
export function isShorthandAmbientModuleSymbol(moduleSymbol: Symbol): boolean {
    return isShorthandAmbientModule(moduleSymbol.valueDeclaration);
}

function isShorthandAmbientModule(node: Node | undefined): boolean {
    // The only kind of module that can be missing a body is a shorthand ambient module.
    return !!node && node.kind === SyntaxKind.ModuleDeclaration && (!(node as ModuleDeclaration).body);
}

/** @internal */
export function isBlockScopedContainerTopLevel(node: Node): boolean {
    return node.kind === SyntaxKind.SourceFile ||
        node.kind === SyntaxKind.ModuleDeclaration ||
        isFunctionLikeOrClassStaticBlockDeclaration(node);
}

/** @internal */
export function isGlobalScopeAugmentation(module: ModuleDeclaration): boolean {
    return !!(module.flags & NodeFlags.GlobalAugmentation);
}

/** @internal */
export function isExternalModuleAugmentation(node: Node): node is AmbientModuleDeclaration {
    return isAmbientModule(node) && isModuleAugmentationExternal(node);
}

/** @internal */
export function isModuleAugmentationExternal(node: AmbientModuleDeclaration) {
    // external module augmentation is a ambient module declaration that is either:
    // - defined in the top level scope and source file is an external module
    // - defined inside ambient module declaration located in the top level scope and source file not an external module
    switch (node.parent.kind) {
        case SyntaxKind.SourceFile:
            return isExternalModule(node.parent);
        case SyntaxKind.ModuleBlock:
            return isAmbientModule(node.parent.parent) && isSourceFile(node.parent.parent.parent) && !isExternalModule(node.parent.parent.parent);
    }
    return false;
}

/** @internal */
export function getNonAugmentationDeclaration(symbol: Symbol) {
    return symbol.declarations?.find(d => !isExternalModuleAugmentation(d) && !(isModuleDeclaration(d) && isGlobalScopeAugmentation(d)));
}

function isCommonJSContainingModuleKind(kind: ModuleKind) {
    return kind === ModuleKind.CommonJS || kind === ModuleKind.Node16 || kind === ModuleKind.NodeNext;
}

/** @internal */
export function isEffectiveExternalModule(node: SourceFile, compilerOptions: CompilerOptions) {
    return isExternalModule(node) || (isCommonJSContainingModuleKind(getEmitModuleKind(compilerOptions)) && !!node.commonJsModuleIndicator);
}

/**
 * Returns whether the source file will be treated as if it were in strict mode at runtime.
 *
 * @internal
 */
export function isEffectiveStrictModeSourceFile(node: SourceFile, compilerOptions: CompilerOptions) {
    // We can only verify strict mode for JS/TS files
    switch (node.scriptKind) {
        case ScriptKind.JS:
        case ScriptKind.TS:
        case ScriptKind.JSX:
        case ScriptKind.TSX:
            break;
        default:
            return false;
    }
    // Strict mode does not matter for declaration files.
    if (node.isDeclarationFile) {
        return false;
    }
    // If `alwaysStrict` is set, then treat the file as strict.
    if (getStrictOptionValue(compilerOptions, "alwaysStrict")) {
        return true;
    }
    // Starting with a "use strict" directive indicates the file is strict.
    if (startsWithUseStrict(node.statements)) {
        return true;
    }
    if (isExternalModule(node) || getIsolatedModules(compilerOptions)) {
        // ECMAScript Modules are always strict.
        if (getEmitModuleKind(compilerOptions) >= ModuleKind.ES2015) {
            return true;
        }
        // Other modules are strict unless otherwise specified.
        return !compilerOptions.noImplicitUseStrict;
    }
    return false;
}

/** @internal */
export function isAmbientPropertyDeclaration(node: PropertyDeclaration) {
    return !!(node.flags & NodeFlags.Ambient) || hasSyntacticModifier(node, ModifierFlags.Ambient);
}

/** @internal */
export function isBlockScope(node: Node, parentNode: Node | undefined): boolean {
    switch (node.kind) {
        case SyntaxKind.SourceFile:
        case SyntaxKind.CaseBlock:
        case SyntaxKind.CatchClause:
        case SyntaxKind.ModuleDeclaration:
        case SyntaxKind.ForStatement:
        case SyntaxKind.ForInStatement:
        case SyntaxKind.ForOfStatement:
        case SyntaxKind.Constructor:
        case SyntaxKind.MethodDeclaration:
        case SyntaxKind.GetAccessor:
        case SyntaxKind.SetAccessor:
        case SyntaxKind.FunctionDeclaration:
        case SyntaxKind.FunctionExpression:
        case SyntaxKind.ArrowFunction:
        case SyntaxKind.PropertyDeclaration:
        case SyntaxKind.ClassStaticBlockDeclaration:
            return true;

        case SyntaxKind.Block:
            // function block is not considered block-scope container
            // see comment in binder.ts: bind(...), case for SyntaxKind.Block
            return !isFunctionLikeOrClassStaticBlockDeclaration(parentNode);
    }

    return false;
}

/** @internal */
export function isDeclarationWithTypeParameters(node: Node): node is DeclarationWithTypeParameters {
    Debug.type<DeclarationWithTypeParameters>(node);
    switch (node.kind) {
        case SyntaxKind.JSDocCallbackTag:
        case SyntaxKind.JSDocTypedefTag:
        case SyntaxKind.JSDocSignature:
            return true;
        default:
            assertType<DeclarationWithTypeParameterChildren>(node);
            return isDeclarationWithTypeParameterChildren(node);
    }
}

/** @internal */
export function isDeclarationWithTypeParameterChildren(node: Node): node is DeclarationWithTypeParameterChildren {
    Debug.type<DeclarationWithTypeParameterChildren>(node);
    switch (node.kind) {
        case SyntaxKind.CallSignature:
        case SyntaxKind.ConstructSignature:
        case SyntaxKind.MethodSignature:
        case SyntaxKind.IndexSignature:
        case SyntaxKind.FunctionType:
        case SyntaxKind.ConstructorType:
        case SyntaxKind.JSDocFunctionType:
        case SyntaxKind.ClassDeclaration:
        case SyntaxKind.ClassExpression:
        case SyntaxKind.InterfaceDeclaration:
        case SyntaxKind.TypeAliasDeclaration:
        case SyntaxKind.JSDocTemplateTag:
        case SyntaxKind.FunctionDeclaration:
        case SyntaxKind.MethodDeclaration:
        case SyntaxKind.Constructor:
        case SyntaxKind.GetAccessor:
        case SyntaxKind.SetAccessor:
        case SyntaxKind.FunctionExpression:
        case SyntaxKind.ArrowFunction:
            return true;
        default:
            assertType<never>(node);
            return false;
    }
}

/** @internal */
export function isAnyImportSyntax(node: Node): node is AnyImportSyntax {
    switch (node.kind) {
        case SyntaxKind.ImportDeclaration:
        case SyntaxKind.ImportEqualsDeclaration:
            return true;
        default:
            return false;
    }
}

/** @internal */
export function isAnyImportOrBareOrAccessedRequire(node: Node): node is AnyImportOrBareOrAccessedRequire {
    return isAnyImportSyntax(node) || isVariableDeclarationInitializedToBareOrAccessedRequire(node);
}

/** @internal */
export function isLateVisibilityPaintedStatement(node: Node): node is LateVisibilityPaintedStatement {
    switch (node.kind) {
        case SyntaxKind.ImportDeclaration:
        case SyntaxKind.ImportEqualsDeclaration:
        case SyntaxKind.VariableStatement:
        case SyntaxKind.ClassDeclaration:
        case SyntaxKind.FunctionDeclaration:
        case SyntaxKind.ModuleDeclaration:
        case SyntaxKind.TypeAliasDeclaration:
        case SyntaxKind.InterfaceDeclaration:
        case SyntaxKind.EnumDeclaration:
            return true;
        default:
            return false;
    }
}

/** @internal */
export function hasPossibleExternalModuleReference(node: Node): node is AnyImportOrReExport | ModuleDeclaration | ImportTypeNode | ImportCall {
    return isAnyImportOrReExport(node) || isModuleDeclaration(node) || isImportTypeNode(node) || isImportCall(node);
}

/** @internal */
export function isAnyImportOrReExport(node: Node): node is AnyImportOrReExport {
    return isAnyImportSyntax(node) || isExportDeclaration(node);
}

/** @internal */
export function getEnclosingContainer(node: Node): Node | undefined {
    return findAncestor(node.parent, n => !!(getContainerFlags(n) & ContainerFlags.IsContainer));
}

// Gets the nearest enclosing block scope container that has the provided node
// as a descendant, that is not the provided node.
/** @internal */
export function getEnclosingBlockScopeContainer(node: Node): Node {
    return findAncestor(node.parent, current => isBlockScope(current, current.parent))!;
}

/** @internal */
export function forEachEnclosingBlockScopeContainer(node: Node, cb: (container: Node) => void): void {
    let container = getEnclosingBlockScopeContainer(node);
    while (container) {
        cb(container);
        container = getEnclosingBlockScopeContainer(container);
    }
}

// Return display name of an identifier
// Computed property names will just be emitted as "[<expr>]", where <expr> is the source
// text of the expression in the computed property.
/** @internal */
export function declarationNameToString(name: DeclarationName | QualifiedName | undefined) {
    return !name || getFullWidth(name) === 0 ? "(Missing)" : getTextOfNode(name);
}

/** @internal */
export function getNameFromIndexInfo(info: IndexInfo): string | undefined {
    return info.declaration ? declarationNameToString(info.declaration.parameters[0].name) : undefined;
}

/** @internal */
export function isComputedNonLiteralName(name: PropertyName): boolean {
    return name.kind === SyntaxKind.ComputedPropertyName && !isStringOrNumericLiteralLike(name.expression);
}

/** @internal */
export function tryGetTextOfPropertyName(name: PropertyName | NoSubstitutionTemplateLiteral | JsxAttributeName): __String | undefined {
    switch (name.kind) {
        case SyntaxKind.Identifier:
        case SyntaxKind.PrivateIdentifier:
            return name.emitNode?.autoGenerate ? undefined : name.escapedText;
        case SyntaxKind.StringLiteral:
        case SyntaxKind.NumericLiteral:
        case SyntaxKind.NoSubstitutionTemplateLiteral:
            return escapeLeadingUnderscores(name.text);
        case SyntaxKind.ComputedPropertyName:
            if (isStringOrNumericLiteralLike(name.expression)) return escapeLeadingUnderscores(name.expression.text);
            return undefined;
        case SyntaxKind.JsxNamespacedName:
            return getEscapedTextOfJsxNamespacedName(name);
        default:
            return Debug.assertNever(name);
    }
}

/** @internal */
export function getTextOfPropertyName(name: PropertyName | NoSubstitutionTemplateLiteral | JsxAttributeName): __String {
    return Debug.checkDefined(tryGetTextOfPropertyName(name));
}

/** @internal */
export function entityNameToString(name: EntityNameOrEntityNameExpression | JSDocMemberName | JsxTagNameExpression | PrivateIdentifier): string {
    switch (name.kind) {
        case SyntaxKind.ThisKeyword:
            return "this";
        case SyntaxKind.PrivateIdentifier:
        case SyntaxKind.Identifier:
            return getFullWidth(name) === 0 ? idText(name) : getTextOfNode(name);
        case SyntaxKind.QualifiedName:
            return entityNameToString(name.left) + "." + entityNameToString(name.right);
        case SyntaxKind.PropertyAccessExpression:
            if (isIdentifier(name.name) || isPrivateIdentifier(name.name)) {
                return entityNameToString(name.expression) + "." + entityNameToString(name.name);
            }
            else {
                return Debug.assertNever(name.name);
            }
        case SyntaxKind.JSDocMemberName:
            return entityNameToString(name.left) + entityNameToString(name.right);
        case SyntaxKind.JsxNamespacedName:
            return entityNameToString(name.namespace) + ":" + entityNameToString(name.name);
        default:
            return Debug.assertNever(name);
    }
}

/** @internal */
export function createDiagnosticForNode(node: Node, message: DiagnosticMessage, ...args: DiagnosticArguments): DiagnosticWithLocation {
    const sourceFile = getSourceFileOfNode(node);
    return createDiagnosticForNodeInSourceFile(sourceFile, node, message, ...args);
}

/** @internal */
export function createDiagnosticForNodeArray(sourceFile: SourceFile, nodes: NodeArray<Node>, message: DiagnosticMessage, ...args: DiagnosticArguments): DiagnosticWithLocation {
    const start = skipTrivia(sourceFile.text, nodes.pos);
    return createFileDiagnostic(sourceFile, start, nodes.end - start, message, ...args);
}

/** @internal */
export function createDiagnosticForNodeInSourceFile(sourceFile: SourceFile, node: Node, message: DiagnosticMessage, ...args: DiagnosticArguments): DiagnosticWithLocation {
    const span = getErrorSpanForNode(sourceFile, node);
    return createFileDiagnostic(sourceFile, span.start, span.length, message, ...args);
}

/** @internal */
export function createDiagnosticForNodeFromMessageChain(sourceFile: SourceFile, node: Node, messageChain: DiagnosticMessageChain, relatedInformation?: DiagnosticRelatedInformation[]): DiagnosticWithLocation {
    const span = getErrorSpanForNode(sourceFile, node);
    return createFileDiagnosticFromMessageChain(sourceFile, span.start, span.length, messageChain, relatedInformation);
}

/** @internal */
export function createDiagnosticForNodeArrayFromMessageChain(sourceFile: SourceFile, nodes: NodeArray<Node>, messageChain: DiagnosticMessageChain, relatedInformation?: DiagnosticRelatedInformation[]): DiagnosticWithLocation {
    const start = skipTrivia(sourceFile.text, nodes.pos);
    return createFileDiagnosticFromMessageChain(sourceFile, start, nodes.end - start, messageChain, relatedInformation);
}

function assertDiagnosticLocation(sourceText: string, start: number, length: number) {
    Debug.assertGreaterThanOrEqual(start, 0);
    Debug.assertGreaterThanOrEqual(length, 0);
    Debug.assertLessThanOrEqual(start, sourceText.length);
    Debug.assertLessThanOrEqual(start + length, sourceText.length);
}

/** @internal */
export function createFileDiagnosticFromMessageChain(file: SourceFile, start: number, length: number, messageChain: DiagnosticMessageChain, relatedInformation?: DiagnosticRelatedInformation[]): DiagnosticWithLocation {
    assertDiagnosticLocation(file.text, start, length);
    return {
        file,
        start,
        length,
        code: messageChain.code,
        category: messageChain.category,
        messageText: messageChain.next ? messageChain : messageChain.messageText,
        relatedInformation,
    };
}

/** @internal */
export function createDiagnosticForFileFromMessageChain(sourceFile: SourceFile, messageChain: DiagnosticMessageChain, relatedInformation?: DiagnosticRelatedInformation[]): DiagnosticWithLocation {
    return {
        file: sourceFile,
        start: 0,
        length: 0,
        code: messageChain.code,
        category: messageChain.category,
        messageText: messageChain.next ? messageChain : messageChain.messageText,
        relatedInformation,
    };
}

/** @internal */
export function createDiagnosticMessageChainFromDiagnostic(diagnostic: DiagnosticRelatedInformation): DiagnosticMessageChain {
    return typeof diagnostic.messageText === "string" ? {
        code: diagnostic.code,
        category: diagnostic.category,
        messageText: diagnostic.messageText,
        next: (diagnostic as DiagnosticMessageChain).next,
    } : diagnostic.messageText;
}

/** @internal */
export function createDiagnosticForRange(sourceFile: SourceFile, range: TextRange, message: DiagnosticMessage): DiagnosticWithLocation {
    return {
        file: sourceFile,
        start: range.pos,
        length: range.end - range.pos,
        code: message.code,
        category: message.category,
        messageText: message.message,
    };
}

/** @internal */
export function getSpanOfTokenAtPosition(sourceFile: SourceFile, pos: number): TextSpan {
    const scanner = createScanner(sourceFile.languageVersion, /*skipTrivia*/ true, sourceFile.languageVariant, sourceFile.text, /*onError*/ undefined, pos);
    scanner.scan();
    const start = scanner.getTokenStart();
    return createTextSpanFromBounds(start, scanner.getTokenEnd());
}

/** @internal */
export function scanTokenAtPosition(sourceFile: SourceFile, pos: number) {
    const scanner = createScanner(sourceFile.languageVersion, /*skipTrivia*/ true, sourceFile.languageVariant, sourceFile.text, /*onError*/ undefined, pos);
    scanner.scan();
    return scanner.getToken();
}

function getErrorSpanForArrowFunction(sourceFile: SourceFile, node: ArrowFunction): TextSpan {
    const pos = skipTrivia(sourceFile.text, node.pos);
    if (node.body && node.body.kind === SyntaxKind.Block) {
        const { line: startLine } = getLineAndCharacterOfPosition(sourceFile, node.body.pos);
        const { line: endLine } = getLineAndCharacterOfPosition(sourceFile, node.body.end);
        if (startLine < endLine) {
            // The arrow function spans multiple lines,
            // make the error span be the first line, inclusive.
            return createTextSpan(pos, getEndLinePosition(startLine, sourceFile) - pos + 1);
        }
    }
    return createTextSpanFromBounds(pos, node.end);
}

/** @internal */
export function getErrorSpanForNode(sourceFile: SourceFile, node: Node): TextSpan {
    let errorNode: Node | undefined = node;
    switch (node.kind) {
        case SyntaxKind.SourceFile: {
            const pos = skipTrivia(sourceFile.text, 0, /*stopAfterLineBreak*/ false);
            if (pos === sourceFile.text.length) {
                // file is empty - return span for the beginning of the file
                return createTextSpan(0, 0);
            }
            return getSpanOfTokenAtPosition(sourceFile, pos);
        }
        // This list is a work in progress. Add missing node kinds to improve their error
        // spans.
        case SyntaxKind.VariableDeclaration:
        case SyntaxKind.BindingElement:
        case SyntaxKind.ClassDeclaration:
        case SyntaxKind.ClassExpression:
        case SyntaxKind.InterfaceDeclaration:
        case SyntaxKind.ModuleDeclaration:
        case SyntaxKind.EnumDeclaration:
        case SyntaxKind.EnumMember:
        case SyntaxKind.FunctionDeclaration:
        case SyntaxKind.FunctionExpression:
        case SyntaxKind.MethodDeclaration:
        case SyntaxKind.GetAccessor:
        case SyntaxKind.SetAccessor:
        case SyntaxKind.TypeAliasDeclaration:
        case SyntaxKind.PropertyDeclaration:
        case SyntaxKind.PropertySignature:
        case SyntaxKind.NamespaceImport:
            errorNode = (node as NamedDeclaration).name;
            break;
        case SyntaxKind.ArrowFunction:
            return getErrorSpanForArrowFunction(sourceFile, node as ArrowFunction);
        case SyntaxKind.CaseClause:
        case SyntaxKind.DefaultClause: {
            const start = skipTrivia(sourceFile.text, (node as CaseOrDefaultClause).pos);
            const end = (node as CaseOrDefaultClause).statements.length > 0 ? (node as CaseOrDefaultClause).statements[0].pos : (node as CaseOrDefaultClause).end;
            return createTextSpanFromBounds(start, end);
        }
        case SyntaxKind.ReturnStatement:
        case SyntaxKind.YieldExpression: {
            const pos = skipTrivia(sourceFile.text, (node as ReturnStatement | YieldExpression).pos);
            return getSpanOfTokenAtPosition(sourceFile, pos);
        }
        case SyntaxKind.SatisfiesExpression: {
            const pos = skipTrivia(sourceFile.text, (node as SatisfiesExpression).expression.end);
            return getSpanOfTokenAtPosition(sourceFile, pos);
        }
        case SyntaxKind.JSDocSatisfiesTag: {
            const pos = skipTrivia(sourceFile.text, (node as JSDocSatisfiesTag).tagName.pos);
            return getSpanOfTokenAtPosition(sourceFile, pos);
        }
    }

    if (errorNode === undefined) {
        // If we don't have a better node, then just set the error on the first token of
        // construct.
        return getSpanOfTokenAtPosition(sourceFile, node.pos);
    }

    Debug.assert(!isJSDoc(errorNode));

    const isMissing = nodeIsMissing(errorNode);
    const pos = isMissing || isJsxText(node)
        ? errorNode.pos
        : skipTrivia(sourceFile.text, errorNode.pos);

    // These asserts should all be satisfied for a properly constructed `errorNode`.
    if (isMissing) {
        Debug.assert(pos === errorNode.pos, "This failure could trigger https://github.com/Microsoft/TypeScript/issues/20809");
        Debug.assert(pos === errorNode.end, "This failure could trigger https://github.com/Microsoft/TypeScript/issues/20809");
    }
    else {
        Debug.assert(pos >= errorNode.pos, "This failure could trigger https://github.com/Microsoft/TypeScript/issues/20809");
        Debug.assert(pos <= errorNode.end, "This failure could trigger https://github.com/Microsoft/TypeScript/issues/20809");
    }

    return createTextSpanFromBounds(pos, errorNode.end);
}

/** @internal */
export function isExternalOrCommonJsModule(file: SourceFile): boolean {
    return (file.externalModuleIndicator || file.commonJsModuleIndicator) !== undefined;
}

/** @internal */
export function isJsonSourceFile(file: SourceFile): file is JsonSourceFile {
    return file.scriptKind === ScriptKind.JSON;
}

/** @internal */
export function isEnumConst(node: EnumDeclaration): boolean {
    return !!(getCombinedModifierFlags(node) & ModifierFlags.Const);
}

/** @internal */
export function isDeclarationReadonly(declaration: Declaration): boolean {
    return !!(getCombinedModifierFlags(declaration) & ModifierFlags.Readonly && !isParameterPropertyDeclaration(declaration, declaration.parent));
}

/**
 * Gets whether a bound `VariableDeclaration` or `VariableDeclarationList` is part of an `await using` declaration.
 * @internal
 */
export function isVarAwaitUsing(node: VariableDeclaration | VariableDeclarationList): boolean {
    return (getCombinedNodeFlags(node) & NodeFlags.BlockScoped) === NodeFlags.AwaitUsing;
}

/**
 * Gets whether a bound `VariableDeclaration` or `VariableDeclarationList` is part of a `using` declaration.
 * @internal
 */
export function isVarUsing(node: VariableDeclaration | VariableDeclarationList): boolean {
    return (getCombinedNodeFlags(node) & NodeFlags.BlockScoped) === NodeFlags.Using;
}

/**
 * Gets whether a bound `VariableDeclaration` or `VariableDeclarationList` is part of a `const` declaration.
 * @internal
 */
export function isVarConst(node: VariableDeclaration | VariableDeclarationList): boolean {
    return (getCombinedNodeFlags(node) & NodeFlags.BlockScoped) === NodeFlags.Const;
}

/**
 * Gets whether a bound `VariableDeclaration` or `VariableDeclarationList` is part of a `let` declaration.
 * @internal
 */
export function isLet(node: Node): boolean {
    return (getCombinedNodeFlags(node) & NodeFlags.BlockScoped) === NodeFlags.Let;
}

/** @internal */
export function isSuperCall(n: Node): n is SuperCall {
    return n.kind === SyntaxKind.CallExpression && (n as CallExpression).expression.kind === SyntaxKind.SuperKeyword;
}

/** @internal */
export function isImportCall(n: Node): n is ImportCall {
    return n.kind === SyntaxKind.CallExpression && (n as CallExpression).expression.kind === SyntaxKind.ImportKeyword;
}

/** @internal */
export function isImportMeta(n: Node): n is ImportMetaProperty {
    return isMetaProperty(n)
        && n.keywordToken === SyntaxKind.ImportKeyword
        && n.name.escapedText === "meta";
}

/** @internal */
export function isLiteralImportTypeNode(n: Node): n is LiteralImportTypeNode {
    return isImportTypeNode(n) && isLiteralTypeNode(n.argument) && isStringLiteral(n.argument.literal);
}

/** @internal */
export function isPrologueDirective(node: Node): node is PrologueDirective {
    return node.kind === SyntaxKind.ExpressionStatement
        && (node as ExpressionStatement).expression.kind === SyntaxKind.StringLiteral;
}

/** @internal */
export function isCustomPrologue(node: Statement) {
    return !!(getEmitFlags(node) & EmitFlags.CustomPrologue);
}

/** @internal */
export function isHoistedFunction(node: Statement) {
    return isCustomPrologue(node)
        && isFunctionDeclaration(node);
}

function isHoistedVariable(node: VariableDeclaration) {
    return isIdentifier(node.name)
        && !node.initializer;
}

/** @internal */
export function isHoistedVariableStatement(node: Statement) {
    return isCustomPrologue(node)
        && isVariableStatement(node)
        && every(node.declarationList.declarations, isHoistedVariable);
}

/** @internal */
export function getLeadingCommentRangesOfNode(node: Node, sourceFileOfNode: SourceFile) {
    return node.kind !== SyntaxKind.JsxText ? getLeadingCommentRanges(sourceFileOfNode.text, node.pos) : undefined;
}

/** @internal */
export function getJSDocCommentRanges(node: Node, text: string) {
    const commentRanges = (node.kind === SyntaxKind.Parameter ||
            node.kind === SyntaxKind.TypeParameter ||
            node.kind === SyntaxKind.FunctionExpression ||
            node.kind === SyntaxKind.ArrowFunction ||
            node.kind === SyntaxKind.ParenthesizedExpression ||
            node.kind === SyntaxKind.VariableDeclaration ||
            node.kind === SyntaxKind.ExportSpecifier) ?
        concatenate(getTrailingCommentRanges(text, node.pos), getLeadingCommentRanges(text, node.pos)) :
        getLeadingCommentRanges(text, node.pos);
    // True if the comment starts with '/**' but not if it is '/**/'
    return filter(commentRanges, comment =>
        text.charCodeAt(comment.pos + 1) === CharacterCodes.asterisk &&
        text.charCodeAt(comment.pos + 2) === CharacterCodes.asterisk &&
        text.charCodeAt(comment.pos + 3) !== CharacterCodes.slash);
}

/** @internal */
export const fullTripleSlashReferencePathRegEx = /^(\/\/\/\s*<reference\s+path\s*=\s*)(('[^']*')|("[^"]*")).*?\/>/;
const fullTripleSlashReferenceTypeReferenceDirectiveRegEx = /^(\/\/\/\s*<reference\s+types\s*=\s*)(('[^']*')|("[^"]*")).*?\/>/;
const fullTripleSlashLibReferenceRegEx = /^(\/\/\/\s*<reference\s+lib\s*=\s*)(('[^']*')|("[^"]*")).*?\/>/;
/** @internal */
export const fullTripleSlashAMDReferencePathRegEx = /^(\/\/\/\s*<amd-dependency\s+path\s*=\s*)(('[^']*')|("[^"]*")).*?\/>/;
const fullTripleSlashAMDModuleRegEx = /^\/\/\/\s*<amd-module\s+.*?\/>/;
const defaultLibReferenceRegEx = /^(\/\/\/\s*<reference\s+no-default-lib\s*=\s*)(('[^']*')|("[^"]*"))\s*\/>/;

export function isPartOfTypeNode(node: Node): boolean {
    if (SyntaxKind.FirstTypeNode <= node.kind && node.kind <= SyntaxKind.LastTypeNode) {
        return true;
    }

    switch (node.kind) {
        case SyntaxKind.AnyKeyword:
        case SyntaxKind.UnknownKeyword:
        case SyntaxKind.NumberKeyword:
        case SyntaxKind.BigIntKeyword:
        case SyntaxKind.StringKeyword:
        case SyntaxKind.BooleanKeyword:
        case SyntaxKind.SymbolKeyword:
        case SyntaxKind.ObjectKeyword:
        case SyntaxKind.UndefinedKeyword:
        case SyntaxKind.NullKeyword:
        case SyntaxKind.NeverKeyword:
            return true;
        case SyntaxKind.VoidKeyword:
            return node.parent.kind !== SyntaxKind.VoidExpression;
        case SyntaxKind.ExpressionWithTypeArguments:
            return isPartOfTypeExpressionWithTypeArguments(node);
        case SyntaxKind.TypeParameter:
            return node.parent.kind === SyntaxKind.MappedType || node.parent.kind === SyntaxKind.InferType;

        // Identifiers and qualified names may be type nodes, depending on their context. Climb
        // above them to find the lowest container
        case SyntaxKind.Identifier:
            // If the identifier is the RHS of a qualified name, then it's a type iff its parent is.
            if (node.parent.kind === SyntaxKind.QualifiedName && (node.parent as QualifiedName).right === node) {
                node = node.parent;
            }
            else if (node.parent.kind === SyntaxKind.PropertyAccessExpression && (node.parent as PropertyAccessExpression).name === node) {
                node = node.parent;
            }
            // At this point, node is either a qualified name or an identifier
            Debug.assert(node.kind === SyntaxKind.Identifier || node.kind === SyntaxKind.QualifiedName || node.kind === SyntaxKind.PropertyAccessExpression, "'node' was expected to be a qualified name, identifier or property access in 'isPartOfTypeNode'.");
            // falls through

        case SyntaxKind.QualifiedName:
        case SyntaxKind.PropertyAccessExpression:
        case SyntaxKind.ThisKeyword: {
            const { parent } = node;
            if (parent.kind === SyntaxKind.TypeQuery) {
                return false;
            }
            if (parent.kind === SyntaxKind.ImportType) {
                return !(parent as ImportTypeNode).isTypeOf;
            }
            // Do not recursively call isPartOfTypeNode on the parent. In the example:
            //
            //     let a: A.B.C;
            //
            // Calling isPartOfTypeNode would consider the qualified name A.B a type node.
            // Only C and A.B.C are type nodes.
            if (SyntaxKind.FirstTypeNode <= parent.kind && parent.kind <= SyntaxKind.LastTypeNode) {
                return true;
            }
            switch (parent.kind) {
                case SyntaxKind.ExpressionWithTypeArguments:
                    return isPartOfTypeExpressionWithTypeArguments(parent);
                case SyntaxKind.TypeParameter:
                    return node === (parent as TypeParameterDeclaration).constraint;
                case SyntaxKind.JSDocTemplateTag:
                    return node === (parent as JSDocTemplateTag).constraint;
                case SyntaxKind.PropertyDeclaration:
                case SyntaxKind.PropertySignature:
                case SyntaxKind.Parameter:
                case SyntaxKind.VariableDeclaration:
                    return node === (parent as HasType).type;
                case SyntaxKind.FunctionDeclaration:
                case SyntaxKind.FunctionExpression:
                case SyntaxKind.ArrowFunction:
                case SyntaxKind.Constructor:
                case SyntaxKind.MethodDeclaration:
                case SyntaxKind.MethodSignature:
                case SyntaxKind.GetAccessor:
                case SyntaxKind.SetAccessor:
                    return node === (parent as FunctionLikeDeclaration).type;
                case SyntaxKind.CallSignature:
                case SyntaxKind.ConstructSignature:
                case SyntaxKind.IndexSignature:
                    return node === (parent as SignatureDeclaration).type;
                case SyntaxKind.TypeAssertionExpression:
                    return node === (parent as TypeAssertion).type;
                case SyntaxKind.CallExpression:
                case SyntaxKind.NewExpression:
                case SyntaxKind.TaggedTemplateExpression:
                    return contains((parent as CallExpression | TaggedTemplateExpression).typeArguments, node);
            }
        }
    }

    return false;
}

function isPartOfTypeExpressionWithTypeArguments(node: Node) {
    return isJSDocImplementsTag(node.parent)
        || isJSDocAugmentsTag(node.parent)
        || isHeritageClause(node.parent) && !isExpressionWithTypeArgumentsInClassExtendsClause(node);
}

/** @internal */
export function isChildOfNodeWithKind(node: Node, kind: SyntaxKind): boolean {
    while (node) {
        if (node.kind === kind) {
            return true;
        }
        node = node.parent;
    }
    return false;
}

// Warning: This has the same semantics as the forEach family of functions,
//          in that traversal terminates in the event that 'visitor' supplies a truthy value.
/** @internal */
export function forEachReturnStatement<T>(body: Block | Statement, visitor: (stmt: ReturnStatement) => T): T | undefined {
    return traverse(body);

    function traverse(node: Node): T | undefined {
        switch (node.kind) {
            case SyntaxKind.ReturnStatement:
                return visitor(node as ReturnStatement);
            case SyntaxKind.CaseBlock:
            case SyntaxKind.Block:
            case SyntaxKind.IfStatement:
            case SyntaxKind.DoStatement:
            case SyntaxKind.WhileStatement:
            case SyntaxKind.ForStatement:
            case SyntaxKind.ForInStatement:
            case SyntaxKind.ForOfStatement:
            case SyntaxKind.WithStatement:
            case SyntaxKind.SwitchStatement:
            case SyntaxKind.CaseClause:
            case SyntaxKind.DefaultClause:
            case SyntaxKind.LabeledStatement:
            case SyntaxKind.TryStatement:
            case SyntaxKind.CatchClause:
                return forEachChild(node, traverse);
        }
    }
}

/** @internal */
export function forEachYieldExpression(body: Block, visitor: (expr: YieldExpression) => void): void {
    return traverse(body);

    function traverse(node: Node): void {
        switch (node.kind) {
            case SyntaxKind.YieldExpression:
                visitor(node as YieldExpression);
                const operand = (node as YieldExpression).expression;
                if (operand) {
                    traverse(operand);
                }
                return;
            case SyntaxKind.EnumDeclaration:
            case SyntaxKind.InterfaceDeclaration:
            case SyntaxKind.ModuleDeclaration:
            case SyntaxKind.TypeAliasDeclaration:
                // These are not allowed inside a generator now, but eventually they may be allowed
                // as local types. Regardless, skip them to avoid the work.
                return;
            default:
                if (isFunctionLike(node)) {
                    if (node.name && node.name.kind === SyntaxKind.ComputedPropertyName) {
                        // Note that we will not include methods/accessors of a class because they would require
                        // first descending into the class. This is by design.
                        traverse(node.name.expression);
                        return;
                    }
                }
                else if (!isPartOfTypeNode(node)) {
                    // This is the general case, which should include mostly expressions and statements.
                    // Also includes NodeArrays.
                    forEachChild(node, traverse);
                }
        }
    }
}

/**
 * Gets the most likely element type for a TypeNode. This is not an exhaustive test
 * as it assumes a rest argument can only be an array type (either T[], or Array<T>).
 *
 * @param node The type node.
 *
 * @internal
 */
export function getRestParameterElementType(node: TypeNode | undefined) {
    if (node && node.kind === SyntaxKind.ArrayType) {
        return (node as ArrayTypeNode).elementType;
    }
    else if (node && node.kind === SyntaxKind.TypeReference) {
        return singleOrUndefined((node as TypeReferenceNode).typeArguments);
    }
    else {
        return undefined;
    }
}

/** @internal */
export function getMembersOfDeclaration(node: Declaration): NodeArray<ClassElement | TypeElement | ObjectLiteralElement> | undefined {
    switch (node.kind) {
        case SyntaxKind.InterfaceDeclaration:
        case SyntaxKind.ClassDeclaration:
        case SyntaxKind.ClassExpression:
        case SyntaxKind.TypeLiteral:
            return (node as ObjectTypeDeclaration).members;
        case SyntaxKind.ObjectLiteralExpression:
            return (node as ObjectLiteralExpression).properties;
    }
}

/** @internal */
export function isVariableLike(node: Node): node is VariableLikeDeclaration {
    if (node) {
        switch (node.kind) {
            case SyntaxKind.BindingElement:
            case SyntaxKind.EnumMember:
            case SyntaxKind.Parameter:
            case SyntaxKind.PropertyAssignment:
            case SyntaxKind.PropertyDeclaration:
            case SyntaxKind.PropertySignature:
            case SyntaxKind.ShorthandPropertyAssignment:
            case SyntaxKind.VariableDeclaration:
                return true;
        }
    }
    return false;
}

/** @internal */
export function isVariableLikeOrAccessor(node: Node): node is AccessorDeclaration | VariableLikeDeclaration {
    return isVariableLike(node) || isAccessor(node);
}

/** @internal */
export function isVariableDeclarationInVariableStatement(node: VariableDeclaration) {
    return node.parent.kind === SyntaxKind.VariableDeclarationList
        && node.parent.parent.kind === SyntaxKind.VariableStatement;
}

/** @internal */
export function isCommonJsExportedExpression(node: Node) {
    if (!isInJSFile(node)) return false;
    return (isObjectLiteralExpression(node.parent) && isBinaryExpression(node.parent.parent) && getAssignmentDeclarationKind(node.parent.parent) === AssignmentDeclarationKind.ModuleExports) ||
        isCommonJsExportPropertyAssignment(node.parent);
}

/** @internal */
export function isCommonJsExportPropertyAssignment(node: Node) {
    if (!isInJSFile(node)) return false;
    return (isBinaryExpression(node) && getAssignmentDeclarationKind(node) === AssignmentDeclarationKind.ExportsProperty);
}

/** @internal */
export function isValidESSymbolDeclaration(node: Node): boolean {
    return (isVariableDeclaration(node) ? isVarConst(node) && isIdentifier(node.name) && isVariableDeclarationInVariableStatement(node) :
        isPropertyDeclaration(node) ? hasEffectiveReadonlyModifier(node) && hasStaticModifier(node) :
        isPropertySignature(node) && hasEffectiveReadonlyModifier(node)) || isCommonJsExportPropertyAssignment(node);
}

/** @internal */
export function introducesArgumentsExoticObject(node: Node) {
    switch (node.kind) {
        case SyntaxKind.MethodDeclaration:
        case SyntaxKind.MethodSignature:
        case SyntaxKind.Constructor:
        case SyntaxKind.GetAccessor:
        case SyntaxKind.SetAccessor:
        case SyntaxKind.FunctionDeclaration:
        case SyntaxKind.FunctionExpression:
            return true;
    }
    return false;
}

/** @internal */
export function unwrapInnermostStatementOfLabel(node: LabeledStatement, beforeUnwrapLabelCallback?: (node: LabeledStatement) => void): Statement {
    while (true) {
        if (beforeUnwrapLabelCallback) {
            beforeUnwrapLabelCallback(node);
        }
        if (node.statement.kind !== SyntaxKind.LabeledStatement) {
            return node.statement;
        }
        node = node.statement as LabeledStatement;
    }
}

/** @internal */
export function isFunctionBlock(node: Node): boolean {
    return node && node.kind === SyntaxKind.Block && isFunctionLike(node.parent);
}

/** @internal */
export function isObjectLiteralMethod(node: Node): node is MethodDeclaration {
    return node && node.kind === SyntaxKind.MethodDeclaration && node.parent.kind === SyntaxKind.ObjectLiteralExpression;
}

/** @internal */
export function isObjectLiteralOrClassExpressionMethodOrAccessor(node: Node): node is MethodDeclaration | AccessorDeclaration {
    return (node.kind === SyntaxKind.MethodDeclaration || node.kind === SyntaxKind.GetAccessor || node.kind === SyntaxKind.SetAccessor) &&
        (node.parent.kind === SyntaxKind.ObjectLiteralExpression ||
            node.parent.kind === SyntaxKind.ClassExpression);
}

/** @internal */
export function isIdentifierTypePredicate(predicate: TypePredicate): predicate is IdentifierTypePredicate {
    return predicate && predicate.kind === TypePredicateKind.Identifier;
}

/** @internal */
export function isThisTypePredicate(predicate: TypePredicate): predicate is ThisTypePredicate {
    return predicate && predicate.kind === TypePredicateKind.This;
}

/** @internal */
export function forEachPropertyAssignment<T>(objectLiteral: ObjectLiteralExpression | undefined, key: string, callback: (property: PropertyAssignment) => T | undefined, key2?: string) {
    return forEach(objectLiteral?.properties, property => {
        if (!isPropertyAssignment(property)) return undefined;
        const propName = tryGetTextOfPropertyName(property.name);
        return key === propName || (key2 && key2 === propName) ?
            callback(property) :
            undefined;
    });
}

/** @internal */
export function getPropertyArrayElementValue(objectLiteral: ObjectLiteralExpression, propKey: string, elementValue: string): StringLiteral | undefined {
    return forEachPropertyAssignment(objectLiteral, propKey, property =>
        isArrayLiteralExpression(property.initializer) ?
            find(property.initializer.elements, (element): element is StringLiteral => isStringLiteral(element) && element.text === elementValue) :
            undefined);
}

/** @internal */
export function getTsConfigObjectLiteralExpression(tsConfigSourceFile: TsConfigSourceFile | undefined): ObjectLiteralExpression | undefined {
    if (tsConfigSourceFile && tsConfigSourceFile.statements.length) {
        const expression = tsConfigSourceFile.statements[0].expression;
        return tryCast(expression, isObjectLiteralExpression);
    }
}

/** @internal */
export function getTsConfigPropArrayElementValue(tsConfigSourceFile: TsConfigSourceFile | undefined, propKey: string, elementValue: string): StringLiteral | undefined {
    return forEachTsConfigPropArray(tsConfigSourceFile, propKey, property =>
        isArrayLiteralExpression(property.initializer) ?
            find(property.initializer.elements, (element): element is StringLiteral => isStringLiteral(element) && element.text === elementValue) :
            undefined);
}

/** @internal */
export function forEachTsConfigPropArray<T>(tsConfigSourceFile: TsConfigSourceFile | undefined, propKey: string, callback: (property: PropertyAssignment) => T | undefined) {
    return forEachPropertyAssignment(getTsConfigObjectLiteralExpression(tsConfigSourceFile), propKey, callback);
}

/** @internal */
export function getContainingFunction(node: Node): SignatureDeclaration | undefined {
    return findAncestor(node.parent, isFunctionLike);
}

/** @internal */
export function getContainingFunctionDeclaration(node: Node): FunctionLikeDeclaration | undefined {
    return findAncestor(node.parent, isFunctionLikeDeclaration);
}

/** @internal */
export function getContainingClass(node: Node): ClassLikeDeclaration | undefined {
    return findAncestor(node.parent, isClassLike);
}

/** @internal */
export function getContainingClassStaticBlock(node: Node): Node | undefined {
    return findAncestor(node.parent, n => {
        if (isClassLike(n) || isFunctionLike(n)) {
            return "quit";
        }
        return isClassStaticBlockDeclaration(n);
    });
}

/** @internal */
export function getContainingFunctionOrClassStaticBlock(node: Node): SignatureDeclaration | ClassStaticBlockDeclaration | undefined {
    return findAncestor(node.parent, isFunctionLikeOrClassStaticBlockDeclaration);
}

/** @internal */
export function getContainingClassExcludingClassDecorators(node: Node): ClassLikeDeclaration | undefined {
    const decorator = findAncestor(node.parent, n => isClassLike(n) ? "quit" : isDecorator(n));
    return decorator && isClassLike(decorator.parent) ? getContainingClass(decorator.parent) : getContainingClass(decorator ?? node);
}

/** @internal */
export type ThisContainer =
    | FunctionDeclaration
    | FunctionExpression
    | ModuleDeclaration
    | ClassStaticBlockDeclaration
    | PropertyDeclaration
    | PropertySignature
    | MethodDeclaration
    | MethodSignature
    | ConstructorDeclaration
    | GetAccessorDeclaration
    | SetAccessorDeclaration
    | CallSignatureDeclaration
    | ConstructSignatureDeclaration
    | IndexSignatureDeclaration
    | EnumDeclaration
    | SourceFile;

/** @internal */
export function getThisContainer(node: Node, includeArrowFunctions: false, includeClassComputedPropertyName: false): ThisContainer;
/** @internal */
export function getThisContainer(node: Node, includeArrowFunctions: false, includeClassComputedPropertyName: boolean): ThisContainer | ComputedPropertyName;
/** @internal */
export function getThisContainer(node: Node, includeArrowFunctions: boolean, includeClassComputedPropertyName: false): ThisContainer | ArrowFunction;
/** @internal */
export function getThisContainer(node: Node, includeArrowFunctions: boolean, includeClassComputedPropertyName: boolean): ThisContainer | ArrowFunction | ComputedPropertyName;
export function getThisContainer(node: Node, includeArrowFunctions: boolean, includeClassComputedPropertyName: boolean) {
    Debug.assert(node.kind !== SyntaxKind.SourceFile);
    while (true) {
        node = node.parent;
        if (!node) {
            return Debug.fail(); // If we never pass in a SourceFile, this should be unreachable, since we'll stop when we reach that.
        }
        switch (node.kind) {
            case SyntaxKind.ComputedPropertyName:
                // If the grandparent node is an object literal (as opposed to a class),
                // then the computed property is not a 'this' container.
                // A computed property name in a class needs to be a this container
                // so that we can error on it.
                if (includeClassComputedPropertyName && isClassLike(node.parent.parent)) {
                    return node as ComputedPropertyName;
                }
                // If this is a computed property, then the parent should not
                // make it a this container. The parent might be a property
                // in an object literal, like a method or accessor. But in order for
                // such a parent to be a this container, the reference must be in
                // the *body* of the container.
                node = node.parent.parent;
                break;
            case SyntaxKind.Decorator:
                // Decorators are always applied outside of the body of a class or method.
                if (node.parent.kind === SyntaxKind.Parameter && isClassElement(node.parent.parent)) {
                    // If the decorator's parent is a Parameter, we resolve the this container from
                    // the grandparent class declaration.
                    node = node.parent.parent;
                }
                else if (isClassElement(node.parent)) {
                    // If the decorator's parent is a class element, we resolve the 'this' container
                    // from the parent class declaration.
                    node = node.parent;
                }
                break;
            case SyntaxKind.ArrowFunction:
                if (!includeArrowFunctions) {
                    continue;
                }
                // falls through

            case SyntaxKind.FunctionDeclaration:
            case SyntaxKind.FunctionExpression:
            case SyntaxKind.ModuleDeclaration:
            case SyntaxKind.ClassStaticBlockDeclaration:
            case SyntaxKind.PropertyDeclaration:
            case SyntaxKind.PropertySignature:
            case SyntaxKind.MethodDeclaration:
            case SyntaxKind.MethodSignature:
            case SyntaxKind.Constructor:
            case SyntaxKind.GetAccessor:
            case SyntaxKind.SetAccessor:
            case SyntaxKind.CallSignature:
            case SyntaxKind.ConstructSignature:
            case SyntaxKind.IndexSignature:
            case SyntaxKind.EnumDeclaration:
            case SyntaxKind.SourceFile:
                return node as ThisContainer | ArrowFunction;
        }
    }
}

/**
 * @returns Whether the node creates a new 'this' scope for its children.
 *
 * @internal
 */
export function isThisContainerOrFunctionBlock(node: Node): boolean {
    switch (node.kind) {
        // Arrow functions use the same scope, but may do so in a "delayed" manner
        // For example, `const getThis = () => this` may be before a super() call in a derived constructor
        case SyntaxKind.ArrowFunction:
        case SyntaxKind.FunctionDeclaration:
        case SyntaxKind.FunctionExpression:
        case SyntaxKind.PropertyDeclaration:
            return true;
        case SyntaxKind.Block:
            switch (node.parent.kind) {
                case SyntaxKind.Constructor:
                case SyntaxKind.MethodDeclaration:
                case SyntaxKind.GetAccessor:
                case SyntaxKind.SetAccessor:
                    // Object properties can have computed names; only method-like bodies start a new scope
                    return true;
                default:
                    return false;
            }
        default:
            return false;
    }
}

/** @internal */
export function isInTopLevelContext(node: Node) {
    // The name of a class or function declaration is a BindingIdentifier in its surrounding scope.
    if (isIdentifier(node) && (isClassDeclaration(node.parent) || isFunctionDeclaration(node.parent)) && node.parent.name === node) {
        node = node.parent;
    }
    const container = getThisContainer(node, /*includeArrowFunctions*/ true, /*includeClassComputedPropertyName*/ false);
    return isSourceFile(container);
}

/** @internal */
export function getNewTargetContainer(node: Node) {
    const container = getThisContainer(node, /*includeArrowFunctions*/ false, /*includeClassComputedPropertyName*/ false);
    if (container) {
        switch (container.kind) {
            case SyntaxKind.Constructor:
            case SyntaxKind.FunctionDeclaration:
            case SyntaxKind.FunctionExpression:
                return container;
        }
    }

    return undefined;
}

/** @internal */
export type SuperContainer =
    | PropertyDeclaration
    | PropertySignature
    | MethodDeclaration
    | MethodSignature
    | ConstructorDeclaration
    | GetAccessorDeclaration
    | SetAccessorDeclaration
    | ClassStaticBlockDeclaration;

/** @internal */
export type SuperContainerOrFunctions =
    | SuperContainer
    | FunctionDeclaration
    | FunctionExpression
    | ArrowFunction;

/**
 * Given an super call/property node, returns the closest node where
 * - a super call/property access is legal in the node and not legal in the parent node the node.
 *   i.e. super call is legal in constructor but not legal in the class body.
 * - the container is an arrow function (so caller might need to call getSuperContainer again in case it needs to climb higher)
 * - a super call/property is definitely illegal in the container (but might be legal in some subnode)
 *   i.e. super property access is illegal in function declaration but can be legal in the statement list
 *
 * @internal
 */
export function getSuperContainer(node: Node, stopOnFunctions: false): SuperContainer | undefined;
/** @internal */
export function getSuperContainer(node: Node, stopOnFunctions: boolean): SuperContainerOrFunctions | undefined;
export function getSuperContainer(node: Node, stopOnFunctions: boolean) {
    while (true) {
        node = node.parent;
        if (!node) {
            return undefined;
        }
        switch (node.kind) {
            case SyntaxKind.ComputedPropertyName:
                node = node.parent;
                break;
            case SyntaxKind.FunctionDeclaration:
            case SyntaxKind.FunctionExpression:
            case SyntaxKind.ArrowFunction:
                if (!stopOnFunctions) {
                    continue;
                }
                // falls through

            case SyntaxKind.PropertyDeclaration:
            case SyntaxKind.PropertySignature:
            case SyntaxKind.MethodDeclaration:
            case SyntaxKind.MethodSignature:
            case SyntaxKind.Constructor:
            case SyntaxKind.GetAccessor:
            case SyntaxKind.SetAccessor:
            case SyntaxKind.ClassStaticBlockDeclaration:
                return node as SuperContainerOrFunctions;
            case SyntaxKind.Decorator:
                // Decorators are always applied outside of the body of a class or method.
                if (node.parent.kind === SyntaxKind.Parameter && isClassElement(node.parent.parent)) {
                    // If the decorator's parent is a Parameter, we resolve the this container from
                    // the grandparent class declaration.
                    node = node.parent.parent;
                }
                else if (isClassElement(node.parent)) {
                    // If the decorator's parent is a class element, we resolve the 'this' container
                    // from the parent class declaration.
                    node = node.parent;
                }
                break;
        }
    }
}

/** @internal */
export function getImmediatelyInvokedFunctionExpression(func: Node): CallExpression | undefined {
    if (func.kind === SyntaxKind.FunctionExpression || func.kind === SyntaxKind.ArrowFunction) {
        let prev = func;
        let parent = func.parent;
        while (parent.kind === SyntaxKind.ParenthesizedExpression) {
            prev = parent;
            parent = parent.parent;
        }
        if (parent.kind === SyntaxKind.CallExpression && (parent as CallExpression).expression === prev) {
            return parent as CallExpression;
        }
    }
}

/** @internal */
export function isSuperOrSuperProperty(node: Node): node is SuperExpression | SuperProperty {
    return node.kind === SyntaxKind.SuperKeyword
        || isSuperProperty(node);
}

/**
 * Determines whether a node is a property or element access expression for `super`.
 *
 * @internal
 */
export function isSuperProperty(node: Node): node is SuperProperty {
    const kind = node.kind;
    return (kind === SyntaxKind.PropertyAccessExpression || kind === SyntaxKind.ElementAccessExpression)
        && (node as PropertyAccessExpression | ElementAccessExpression).expression.kind === SyntaxKind.SuperKeyword;
}

/**
 * Determines whether a node is a property or element access expression for `this`.
 *
 * @internal
 */
export function isThisProperty(node: Node): boolean {
    const kind = node.kind;
    return (kind === SyntaxKind.PropertyAccessExpression || kind === SyntaxKind.ElementAccessExpression)
        && (node as PropertyAccessExpression | ElementAccessExpression).expression.kind === SyntaxKind.ThisKeyword;
}

/** @internal */
export function isThisInitializedDeclaration(node: Node | undefined): boolean {
    return !!node && isVariableDeclaration(node) && node.initializer?.kind === SyntaxKind.ThisKeyword;
}

/** @internal */
export function isThisInitializedObjectBindingExpression(node: Node | undefined): boolean {
    return !!node
        && (isShorthandPropertyAssignment(node) || isPropertyAssignment(node))
        && isBinaryExpression(node.parent.parent)
        && node.parent.parent.operatorToken.kind === SyntaxKind.EqualsToken
        && node.parent.parent.right.kind === SyntaxKind.ThisKeyword;
}

/** @internal */
export function getEntityNameFromTypeNode(node: TypeNode): EntityNameOrEntityNameExpression | undefined {
    switch (node.kind) {
        case SyntaxKind.TypeReference:
            return (node as TypeReferenceNode).typeName;

        case SyntaxKind.ExpressionWithTypeArguments:
            return isEntityNameExpression((node as ExpressionWithTypeArguments).expression)
                ? (node as ExpressionWithTypeArguments).expression as EntityNameExpression
                : undefined;

        // TODO(rbuckton): These aren't valid TypeNodes, but we treat them as such because of `isPartOfTypeNode`, which returns `true` for things that aren't `TypeNode`s.
        case SyntaxKind.Identifier as TypeNodeSyntaxKind:
        case SyntaxKind.QualifiedName as TypeNodeSyntaxKind:
            return (node as Node as EntityName);
    }

    return undefined;
}

/** @internal */
export function getInvokedExpression(node: CallLikeExpression): Expression | JsxTagNameExpression {
    switch (node.kind) {
        case SyntaxKind.TaggedTemplateExpression:
            return node.tag;
        case SyntaxKind.JsxOpeningElement:
        case SyntaxKind.JsxSelfClosingElement:
            return node.tagName;
        case SyntaxKind.BinaryExpression:
            return node.right;
        default:
            return node.expression;
    }
}

/** @internal */
export function nodeCanBeDecorated(useLegacyDecorators: boolean, node: ClassDeclaration): true;
/** @internal */
export function nodeCanBeDecorated(useLegacyDecorators: boolean, node: ClassExpression): boolean;
/** @internal */
export function nodeCanBeDecorated(useLegacyDecorators: boolean, node: ClassElement, parent: Node): boolean;
/** @internal */
export function nodeCanBeDecorated(useLegacyDecorators: boolean, node: Node, parent: Node, grandparent: Node): boolean;
/** @internal */
export function nodeCanBeDecorated(useLegacyDecorators: boolean, node: Node, parent?: Node, grandparent?: Node): boolean {
    // private names cannot be used with decorators yet
    if (useLegacyDecorators && isNamedDeclaration(node) && isPrivateIdentifier(node.name)) {
        return false;
    }

    switch (node.kind) {
        case SyntaxKind.ClassDeclaration:
            // class declarations are valid targets
            return true;

        case SyntaxKind.ClassExpression:
            // class expressions are valid targets for native decorators
            return !useLegacyDecorators;

        case SyntaxKind.PropertyDeclaration:
            // property declarations are valid if their parent is a class declaration.
            return parent !== undefined
                && (useLegacyDecorators ? isClassDeclaration(parent) : isClassLike(parent) && !hasAbstractModifier(node) && !hasAmbientModifier(node));

        case SyntaxKind.GetAccessor:
        case SyntaxKind.SetAccessor:
        case SyntaxKind.MethodDeclaration:
            // if this method has a body and its parent is a class declaration, this is a valid target.
            return (node as FunctionLikeDeclaration).body !== undefined
                && parent !== undefined
                && (useLegacyDecorators ? isClassDeclaration(parent) : isClassLike(parent));

        case SyntaxKind.Parameter:
            // TODO(rbuckton): Parameter decorator support for ES decorators must wait until it is standardized
            if (!useLegacyDecorators) return false;
            // if the parameter's parent has a body and its grandparent is a class declaration, this is a valid target.
            return parent !== undefined
                && (parent as FunctionLikeDeclaration).body !== undefined
                && (parent.kind === SyntaxKind.Constructor
                    || parent.kind === SyntaxKind.MethodDeclaration
                    || parent.kind === SyntaxKind.SetAccessor)
                && getThisParameter(parent as FunctionLikeDeclaration) !== node
                && grandparent !== undefined
                && grandparent.kind === SyntaxKind.ClassDeclaration;
    }

    return false;
}

/** @internal */
export function nodeIsDecorated(useLegacyDecorators: boolean, node: ClassDeclaration | ClassExpression): boolean;
/** @internal */
export function nodeIsDecorated(useLegacyDecorators: boolean, node: ClassElement, parent: Node): boolean;
/** @internal */
export function nodeIsDecorated(useLegacyDecorators: boolean, node: Node, parent: Node, grandparent: Node): boolean;
/** @internal */
export function nodeIsDecorated(useLegacyDecorators: boolean, node: Node, parent?: Node, grandparent?: Node): boolean {
    return hasDecorators(node)
        && nodeCanBeDecorated(useLegacyDecorators, node, parent!, grandparent!);
}

/** @internal */
export function nodeOrChildIsDecorated(useLegacyDecorators: boolean, node: ClassDeclaration | ClassExpression): boolean;
/** @internal */
export function nodeOrChildIsDecorated(useLegacyDecorators: boolean, node: ClassElement, parent: Node): boolean;
/** @internal */
export function nodeOrChildIsDecorated(useLegacyDecorators: boolean, node: Node, parent: Node, grandparent: Node): boolean;
/** @internal */
export function nodeOrChildIsDecorated(useLegacyDecorators: boolean, node: Node, parent?: Node, grandparent?: Node): boolean {
    return nodeIsDecorated(useLegacyDecorators, node, parent!, grandparent!)
        || childIsDecorated(useLegacyDecorators, node, parent!);
}

/** @internal */
export function childIsDecorated(useLegacyDecorators: boolean, node: ClassDeclaration | ClassExpression): boolean;
/** @internal */
export function childIsDecorated(useLegacyDecorators: boolean, node: Node, parent: Node): boolean;
/** @internal */
export function childIsDecorated(useLegacyDecorators: boolean, node: Node, parent?: Node): boolean {
    switch (node.kind) {
        case SyntaxKind.ClassDeclaration:
            return some((node as ClassDeclaration).members, m => nodeOrChildIsDecorated(useLegacyDecorators, m, node, parent!));
        case SyntaxKind.ClassExpression:
            return !useLegacyDecorators && some((node as ClassExpression).members, m => nodeOrChildIsDecorated(useLegacyDecorators, m, node, parent!));
        case SyntaxKind.MethodDeclaration:
        case SyntaxKind.SetAccessor:
        case SyntaxKind.Constructor:
            return some((node as FunctionLikeDeclaration).parameters, p => nodeIsDecorated(useLegacyDecorators, p, node, parent!));
        default:
            return false;
    }
}

/** @internal */
export function classOrConstructorParameterIsDecorated(useLegacyDecorators: boolean, node: ClassDeclaration | ClassExpression): boolean {
    if (nodeIsDecorated(useLegacyDecorators, node)) return true;
    const constructor = getFirstConstructorWithBody(node);
    return !!constructor && childIsDecorated(useLegacyDecorators, constructor, node);
}

/** @internal */
export function classElementOrClassElementParameterIsDecorated(useLegacyDecorators: boolean, node: ClassElement, parent: ClassDeclaration | ClassExpression): boolean {
    let parameters: NodeArray<ParameterDeclaration> | undefined;
    if (isAccessor(node)) {
        const { firstAccessor, secondAccessor, setAccessor } = getAllAccessorDeclarations(parent.members, node);
        const firstAccessorWithDecorators = hasDecorators(firstAccessor) ? firstAccessor :
            secondAccessor && hasDecorators(secondAccessor) ? secondAccessor :
            undefined;
        if (!firstAccessorWithDecorators || node !== firstAccessorWithDecorators) {
            return false;
        }
        parameters = setAccessor?.parameters;
    }
    else if (isMethodDeclaration(node)) {
        parameters = node.parameters;
    }
    if (nodeIsDecorated(useLegacyDecorators, node, parent)) {
        return true;
    }
    if (parameters) {
        for (const parameter of parameters) {
            if (parameterIsThisKeyword(parameter)) continue;
            if (nodeIsDecorated(useLegacyDecorators, parameter, node, parent)) return true;
        }
    }
    return false;
}

/** @internal */
export function isEmptyStringLiteral(node: StringLiteral): boolean {
    if (node.textSourceNode) {
        switch (node.textSourceNode.kind) {
            case SyntaxKind.StringLiteral:
                return isEmptyStringLiteral(node.textSourceNode);
            case SyntaxKind.NoSubstitutionTemplateLiteral:
                return node.text === "";
        }
        return false;
    }
    return node.text === "";
}

/** @internal */
export function isJSXTagName(node: Node) {
    const { parent } = node;
    if (
        parent.kind === SyntaxKind.JsxOpeningElement ||
        parent.kind === SyntaxKind.JsxSelfClosingElement ||
        parent.kind === SyntaxKind.JsxClosingElement
    ) {
        return (parent as JsxOpeningLikeElement).tagName === node;
    }
    return false;
}

/** @internal */
export function isExpressionNode(node: Node): boolean {
    switch (node.kind) {
        case SyntaxKind.SuperKeyword:
        case SyntaxKind.NullKeyword:
        case SyntaxKind.TrueKeyword:
        case SyntaxKind.FalseKeyword:
        case SyntaxKind.RegularExpressionLiteral:
        case SyntaxKind.ArrayLiteralExpression:
        case SyntaxKind.ObjectLiteralExpression:
        case SyntaxKind.PropertyAccessExpression:
        case SyntaxKind.ElementAccessExpression:
        case SyntaxKind.CallExpression:
        case SyntaxKind.NewExpression:
        case SyntaxKind.TaggedTemplateExpression:
        case SyntaxKind.AsExpression:
        case SyntaxKind.TypeAssertionExpression:
        case SyntaxKind.SatisfiesExpression:
        case SyntaxKind.NonNullExpression:
        case SyntaxKind.ParenthesizedExpression:
        case SyntaxKind.FunctionExpression:
        case SyntaxKind.ClassExpression:
        case SyntaxKind.ArrowFunction:
        case SyntaxKind.VoidExpression:
        case SyntaxKind.DeleteExpression:
        case SyntaxKind.TypeOfExpression:
        case SyntaxKind.PrefixUnaryExpression:
        case SyntaxKind.PostfixUnaryExpression:
        case SyntaxKind.BinaryExpression:
        case SyntaxKind.ConditionalExpression:
        case SyntaxKind.SpreadElement:
        case SyntaxKind.TemplateExpression:
        case SyntaxKind.OmittedExpression:
        case SyntaxKind.JsxElement:
        case SyntaxKind.JsxSelfClosingElement:
        case SyntaxKind.JsxFragment:
        case SyntaxKind.YieldExpression:
        case SyntaxKind.AwaitExpression:
        case SyntaxKind.MetaProperty:
            return true;
        case SyntaxKind.ExpressionWithTypeArguments:
            return !isHeritageClause(node.parent) && !isJSDocAugmentsTag(node.parent);
        case SyntaxKind.QualifiedName:
            while (node.parent.kind === SyntaxKind.QualifiedName) {
                node = node.parent;
            }
            return node.parent.kind === SyntaxKind.TypeQuery || isJSDocLinkLike(node.parent) || isJSDocNameReference(node.parent) || isJSDocMemberName(node.parent) || isJSXTagName(node);
        case SyntaxKind.JSDocMemberName:
            while (isJSDocMemberName(node.parent)) {
                node = node.parent;
            }
            return node.parent.kind === SyntaxKind.TypeQuery || isJSDocLinkLike(node.parent) || isJSDocNameReference(node.parent) || isJSDocMemberName(node.parent) || isJSXTagName(node);
        case SyntaxKind.PrivateIdentifier:
            return isBinaryExpression(node.parent) && node.parent.left === node && node.parent.operatorToken.kind === SyntaxKind.InKeyword;
        case SyntaxKind.Identifier:
            if (node.parent.kind === SyntaxKind.TypeQuery || isJSDocLinkLike(node.parent) || isJSDocNameReference(node.parent) || isJSDocMemberName(node.parent) || isJSXTagName(node)) {
                return true;
            }
            // falls through

        case SyntaxKind.NumericLiteral:
        case SyntaxKind.BigIntLiteral:
        case SyntaxKind.StringLiteral:
        case SyntaxKind.NoSubstitutionTemplateLiteral:
        case SyntaxKind.ThisKeyword:
            return isInExpressionContext(node);
        default:
            return false;
    }
}

/** @internal */
export function isInExpressionContext(node: Node): boolean {
    const { parent } = node;
    switch (parent.kind) {
        case SyntaxKind.VariableDeclaration:
        case SyntaxKind.Parameter:
        case SyntaxKind.PropertyDeclaration:
        case SyntaxKind.PropertySignature:
        case SyntaxKind.EnumMember:
        case SyntaxKind.PropertyAssignment:
        case SyntaxKind.BindingElement:
            return (parent as HasInitializer).initializer === node;
        case SyntaxKind.ExpressionStatement:
        case SyntaxKind.IfStatement:
        case SyntaxKind.DoStatement:
        case SyntaxKind.WhileStatement:
        case SyntaxKind.ReturnStatement:
        case SyntaxKind.WithStatement:
        case SyntaxKind.SwitchStatement:
        case SyntaxKind.CaseClause:
        case SyntaxKind.ThrowStatement:
            return (parent as ExpressionStatement).expression === node;
        case SyntaxKind.ForStatement:
            const forStatement = parent as ForStatement;
            return (forStatement.initializer === node && forStatement.initializer.kind !== SyntaxKind.VariableDeclarationList) ||
                forStatement.condition === node ||
                forStatement.incrementor === node;
        case SyntaxKind.ForInStatement:
        case SyntaxKind.ForOfStatement:
            const forInOrOfStatement = parent as ForInOrOfStatement;
            return (forInOrOfStatement.initializer === node && forInOrOfStatement.initializer.kind !== SyntaxKind.VariableDeclarationList) ||
                forInOrOfStatement.expression === node;
        case SyntaxKind.TypeAssertionExpression:
        case SyntaxKind.AsExpression:
            return node === (parent as AssertionExpression).expression;
        case SyntaxKind.TemplateSpan:
            return node === (parent as TemplateSpan).expression;
        case SyntaxKind.ComputedPropertyName:
            return node === (parent as ComputedPropertyName).expression;
        case SyntaxKind.Decorator:
        case SyntaxKind.JsxExpression:
        case SyntaxKind.JsxSpreadAttribute:
        case SyntaxKind.SpreadAssignment:
            return true;
        case SyntaxKind.ExpressionWithTypeArguments:
            return (parent as ExpressionWithTypeArguments).expression === node && !isPartOfTypeNode(parent);
        case SyntaxKind.ShorthandPropertyAssignment:
            return (parent as ShorthandPropertyAssignment).objectAssignmentInitializer === node;
        case SyntaxKind.SatisfiesExpression:
            return node === (parent as SatisfiesExpression).expression;
        default:
            return isExpressionNode(parent);
    }
}

/** @internal */
export function isPartOfTypeQuery(node: Node) {
    while (node.kind === SyntaxKind.QualifiedName || node.kind === SyntaxKind.Identifier) {
        node = node.parent;
    }
    return node.kind === SyntaxKind.TypeQuery;
}

/** @internal */
export function isNamespaceReexportDeclaration(node: Node): boolean {
    return isNamespaceExport(node) && !!node.parent.moduleSpecifier;
}

/** @internal */
export function isExternalModuleImportEqualsDeclaration(node: Node): node is ImportEqualsDeclaration & { moduleReference: ExternalModuleReference; } {
    return node.kind === SyntaxKind.ImportEqualsDeclaration && (node as ImportEqualsDeclaration).moduleReference.kind === SyntaxKind.ExternalModuleReference;
}

/** @internal */
export function getExternalModuleImportEqualsDeclarationExpression(node: Node) {
    Debug.assert(isExternalModuleImportEqualsDeclaration(node));
    return ((node as ImportEqualsDeclaration).moduleReference as ExternalModuleReference).expression;
}

/** @internal */
export function getExternalModuleRequireArgument(node: Node) {
    return isVariableDeclarationInitializedToBareOrAccessedRequire(node) && (getLeftmostAccessExpression(node.initializer) as CallExpression).arguments[0] as StringLiteral;
}

/** @internal */
export function isInternalModuleImportEqualsDeclaration(node: Node): node is ImportEqualsDeclaration {
    return node.kind === SyntaxKind.ImportEqualsDeclaration && (node as ImportEqualsDeclaration).moduleReference.kind !== SyntaxKind.ExternalModuleReference;
}

/** @internal */
export function isSourceFileJS(file: SourceFile): boolean {
    return isInJSFile(file);
}

/** @internal */
export function isSourceFileNotJS(file: SourceFile): boolean {
    return !isInJSFile(file);
}

/** @internal */
export function isInJSFile(node: Node | undefined): boolean {
    return !!node && !!(node.flags & NodeFlags.JavaScriptFile);
}

/** @internal */
export function isInJsonFile(node: Node | undefined): boolean {
    return !!node && !!(node.flags & NodeFlags.JsonFile);
}

/** @internal */
export function isSourceFileNotJson(file: SourceFile) {
    return !isJsonSourceFile(file);
}

/** @internal */
export function isInJSDoc(node: Node | undefined): boolean {
    return !!node && !!(node.flags & NodeFlags.JSDoc);
}

/** @internal */
export function isJSDocIndexSignature(node: TypeReferenceNode | ExpressionWithTypeArguments) {
    return isTypeReferenceNode(node) &&
        isIdentifier(node.typeName) &&
        node.typeName.escapedText === "Object" &&
        node.typeArguments && node.typeArguments.length === 2 &&
        (node.typeArguments[0].kind === SyntaxKind.StringKeyword || node.typeArguments[0].kind === SyntaxKind.NumberKeyword);
}

/**
 * Returns true if the node is a CallExpression to the identifier 'require' with
 * exactly one argument (of the form 'require("name")').
 * This function does not test if the node is in a JavaScript file or not.
 *
 * @internal
 */
export function isRequireCall(callExpression: Node, requireStringLiteralLikeArgument: true): callExpression is RequireOrImportCall & { expression: Identifier; arguments: [StringLiteralLike]; };
/** @internal */
export function isRequireCall(callExpression: Node, requireStringLiteralLikeArgument: boolean): callExpression is CallExpression;
/** @internal */
export function isRequireCall(callExpression: Node, requireStringLiteralLikeArgument: boolean): callExpression is CallExpression {
    if (callExpression.kind !== SyntaxKind.CallExpression) {
        return false;
    }
    const { expression, arguments: args } = callExpression as CallExpression;

    if (expression.kind !== SyntaxKind.Identifier || (expression as Identifier).escapedText !== "require") {
        return false;
    }

    if (args.length !== 1) {
        return false;
    }
    const arg = args[0];
    return !requireStringLiteralLikeArgument || isStringLiteralLike(arg);
}

/**
 * Returns true if the node is a VariableDeclaration initialized to a require call (see `isRequireCall`).
 * This function does not test if the node is in a JavaScript file or not.
 *
 * @internal
 */
export function isVariableDeclarationInitializedToRequire(node: Node): node is VariableDeclarationInitializedTo<RequireOrImportCall> {
    return isVariableDeclarationInitializedWithRequireHelper(node, /*allowAccessedRequire*/ false);
}

/**
 * Like {@link isVariableDeclarationInitializedToRequire} but allows things like `require("...").foo.bar` or `require("...")["baz"]`.
 *
 * @internal
 */
export function isVariableDeclarationInitializedToBareOrAccessedRequire(node: Node): node is VariableDeclarationInitializedTo<RequireOrImportCall | AccessExpression> {
    return isVariableDeclarationInitializedWithRequireHelper(node, /*allowAccessedRequire*/ true);
}

/** @internal */
export function isBindingElementOfBareOrAccessedRequire(node: Node): node is BindingElementOfBareOrAccessedRequire {
    return isBindingElement(node) && isVariableDeclarationInitializedToBareOrAccessedRequire(node.parent.parent);
}

function isVariableDeclarationInitializedWithRequireHelper(node: Node, allowAccessedRequire: boolean) {
    return isVariableDeclaration(node) &&
        !!node.initializer &&
        isRequireCall(allowAccessedRequire ? getLeftmostAccessExpression(node.initializer) : node.initializer, /*requireStringLiteralLikeArgument*/ true);
}

/** @internal */
export function isRequireVariableStatement(node: Node): node is RequireVariableStatement {
    return isVariableStatement(node)
        && node.declarationList.declarations.length > 0
        && every(node.declarationList.declarations, decl => isVariableDeclarationInitializedToRequire(decl));
}

/** @internal */
export function isSingleOrDoubleQuote(charCode: number) {
    return charCode === CharacterCodes.singleQuote || charCode === CharacterCodes.doubleQuote;
}

/** @internal */
export function isStringDoubleQuoted(str: StringLiteralLike, sourceFile: SourceFile): boolean {
    return getSourceTextOfNodeFromSourceFile(sourceFile, str).charCodeAt(0) === CharacterCodes.doubleQuote;
}

/** @internal */
export function isAssignmentDeclaration(decl: Declaration) {
    return isBinaryExpression(decl) || isAccessExpression(decl) || isIdentifier(decl) || isCallExpression(decl);
}

/**
 * Get the initializer, taking into account defaulted Javascript initializers
 *
 * @internal
 */
export function getEffectiveInitializer(node: HasExpressionInitializer) {
    if (
        isInJSFile(node) && node.initializer &&
        isBinaryExpression(node.initializer) &&
        (node.initializer.operatorToken.kind === SyntaxKind.BarBarToken || node.initializer.operatorToken.kind === SyntaxKind.QuestionQuestionToken) &&
        node.name && isEntityNameExpression(node.name) && isSameEntityName(node.name, node.initializer.left)
    ) {
        return node.initializer.right;
    }
    return node.initializer;
}

/**
 * Get the declaration initializer when it is container-like (See getExpandoInitializer).
 *
 * @internal
 */
export function getDeclaredExpandoInitializer(node: HasExpressionInitializer) {
    const init = getEffectiveInitializer(node);
    return init && getExpandoInitializer(init, isPrototypeAccess(node.name));
}

function hasExpandoValueProperty(node: ObjectLiteralExpression, isPrototypeAssignment: boolean) {
    return forEach(node.properties, p =>
        isPropertyAssignment(p) &&
        isIdentifier(p.name) &&
        p.name.escapedText === "value" &&
        p.initializer &&
        getExpandoInitializer(p.initializer, isPrototypeAssignment));
}

/**
 * Get the assignment 'initializer' -- the righthand side-- when the initializer is container-like (See getExpandoInitializer).
 * We treat the right hand side of assignments with container-like initializers as declarations.
 *
 * @internal
 */
export function getAssignedExpandoInitializer(node: Node | undefined): Expression | undefined {
    if (node && node.parent && isBinaryExpression(node.parent) && node.parent.operatorToken.kind === SyntaxKind.EqualsToken) {
        const isPrototypeAssignment = isPrototypeAccess(node.parent.left);
        return getExpandoInitializer(node.parent.right, isPrototypeAssignment) ||
            getDefaultedExpandoInitializer(node.parent.left, node.parent.right, isPrototypeAssignment);
    }
    if (node && isCallExpression(node) && isBindableObjectDefinePropertyCall(node)) {
        const result = hasExpandoValueProperty(node.arguments[2], node.arguments[1].text === "prototype");
        if (result) {
            return result;
        }
    }
}

/**
 * Recognized expando initializers are:
 * 1. (function() {})() -- IIFEs
 * 2. function() { } -- Function expressions
 * 3. class { } -- Class expressions
 * 4. {} -- Empty object literals
 * 5. { ... } -- Non-empty object literals, when used to initialize a prototype, like `C.prototype = { m() { } }`
 *
 * This function returns the provided initializer, or undefined if it is not valid.
 *
 * @internal
 */
export function getExpandoInitializer(initializer: Node, isPrototypeAssignment: boolean): Expression | undefined {
    if (isCallExpression(initializer)) {
        const e = skipParentheses(initializer.expression);
        return e.kind === SyntaxKind.FunctionExpression || e.kind === SyntaxKind.ArrowFunction ? initializer : undefined;
    }
    if (
        initializer.kind === SyntaxKind.FunctionExpression ||
        initializer.kind === SyntaxKind.ClassExpression ||
        initializer.kind === SyntaxKind.ArrowFunction
    ) {
        return initializer as Expression;
    }
    if (isObjectLiteralExpression(initializer) && (initializer.properties.length === 0 || isPrototypeAssignment)) {
        return initializer;
    }
}

/**
 * A defaulted expando initializer matches the pattern
 * `Lhs = Lhs || ExpandoInitializer`
 * or `var Lhs = Lhs || ExpandoInitializer`
 *
 * The second Lhs is required to be the same as the first except that it may be prefixed with
 * 'window.', 'global.' or 'self.' The second Lhs is otherwise ignored by the binder and checker.
 */
function getDefaultedExpandoInitializer(name: Expression, initializer: Expression, isPrototypeAssignment: boolean) {
    const e = isBinaryExpression(initializer)
        && (initializer.operatorToken.kind === SyntaxKind.BarBarToken || initializer.operatorToken.kind === SyntaxKind.QuestionQuestionToken)
        && getExpandoInitializer(initializer.right, isPrototypeAssignment);
    if (e && isSameEntityName(name, initializer.left)) {
        return e;
    }
}

/** @internal */
export function isDefaultedExpandoInitializer(node: BinaryExpression) {
    const name = isVariableDeclaration(node.parent) ? node.parent.name :
        isBinaryExpression(node.parent) && node.parent.operatorToken.kind === SyntaxKind.EqualsToken ? node.parent.left :
        undefined;
    return name && getExpandoInitializer(node.right, isPrototypeAccess(name)) && isEntityNameExpression(name) && isSameEntityName(name, node.left);
}

/**
 * Given an expando initializer, return its declaration name, or the left-hand side of the assignment if it's part of an assignment declaration.
 *
 * @internal
 */
export function getNameOfExpando(node: Declaration): DeclarationName | undefined {
    if (isBinaryExpression(node.parent)) {
        const parent = ((node.parent.operatorToken.kind === SyntaxKind.BarBarToken || node.parent.operatorToken.kind === SyntaxKind.QuestionQuestionToken) && isBinaryExpression(node.parent.parent)) ? node.parent.parent : node.parent;
        if (parent.operatorToken.kind === SyntaxKind.EqualsToken && isIdentifier(parent.left)) {
            return parent.left;
        }
    }
    else if (isVariableDeclaration(node.parent)) {
        return node.parent.name;
    }
}

/**
 * Is the 'declared' name the same as the one in the initializer?
 * @return true for identical entity names, as well as ones where the initializer is prefixed with
 * 'window', 'self' or 'global'. For example:
 *
 * var my = my || {}
 * var min = window.min || {}
 * my.app = self.my.app || class { }
 *
 * @internal
 */
export function isSameEntityName(name: Expression, initializer: Expression): boolean {
    if (isPropertyNameLiteral(name) && isPropertyNameLiteral(initializer)) {
        return getTextOfIdentifierOrLiteral(name) === getTextOfIdentifierOrLiteral(initializer);
    }
    if (
        isMemberName(name) && isLiteralLikeAccess(initializer) &&
        (initializer.expression.kind === SyntaxKind.ThisKeyword ||
            isIdentifier(initializer.expression) &&
                (initializer.expression.escapedText === "window" ||
                    initializer.expression.escapedText === "self" ||
                    initializer.expression.escapedText === "global"))
    ) {
        return isSameEntityName(name, getNameOrArgument(initializer));
    }
    if (isLiteralLikeAccess(name) && isLiteralLikeAccess(initializer)) {
        return getElementOrPropertyAccessName(name) === getElementOrPropertyAccessName(initializer)
            && isSameEntityName(name.expression, initializer.expression);
    }
    return false;
}

/** @internal */
export function getRightMostAssignedExpression(node: Expression): Expression {
    while (isAssignmentExpression(node, /*excludeCompoundAssignment*/ true)) {
        node = node.right;
    }
    return node;
}

/** @internal */
export function isExportsIdentifier(node: Node) {
    return isIdentifier(node) && node.escapedText === "exports";
}

/** @internal */
export function isModuleIdentifier(node: Node) {
    return isIdentifier(node) && node.escapedText === "module";
}

/** @internal */
export function isModuleExportsAccessExpression(node: Node): node is LiteralLikeElementAccessExpression & { expression: Identifier; } {
    return (isPropertyAccessExpression(node) || isLiteralLikeElementAccess(node))
        && isModuleIdentifier(node.expression)
        && getElementOrPropertyAccessName(node) === "exports";
}

/// Given a BinaryExpression, returns SpecialPropertyAssignmentKind for the various kinds of property
/// assignments we treat as special in the binder
/** @internal */
export function getAssignmentDeclarationKind(expr: BinaryExpression | CallExpression): AssignmentDeclarationKind {
    const special = getAssignmentDeclarationKindWorker(expr);
    return special === AssignmentDeclarationKind.Property || isInJSFile(expr) ? special : AssignmentDeclarationKind.None;
}

/** @internal */
export function isBindableObjectDefinePropertyCall(expr: CallExpression): expr is BindableObjectDefinePropertyCall {
    return length(expr.arguments) === 3 &&
        isPropertyAccessExpression(expr.expression) &&
        isIdentifier(expr.expression.expression) &&
        idText(expr.expression.expression) === "Object" &&
        idText(expr.expression.name) === "defineProperty" &&
        isStringOrNumericLiteralLike(expr.arguments[1]) &&
        isBindableStaticNameExpression(expr.arguments[0], /*excludeThisKeyword*/ true);
}

/**
 * x.y OR x[0]
 *
 * @internal
 */
export function isLiteralLikeAccess(node: Node): node is LiteralLikeElementAccessExpression | PropertyAccessExpression {
    return isPropertyAccessExpression(node) || isLiteralLikeElementAccess(node);
}

/**
 * x[0] OR x['a'] OR x[Symbol.y]
 *
 * @internal
 */
export function isLiteralLikeElementAccess(node: Node): node is LiteralLikeElementAccessExpression {
    return isElementAccessExpression(node) && isStringOrNumericLiteralLike(node.argumentExpression);
}

/**
 * Any series of property and element accesses.
 *
 * @internal
 */
export function isBindableStaticAccessExpression(node: Node, excludeThisKeyword?: boolean): node is BindableStaticAccessExpression {
    return isPropertyAccessExpression(node) && (!excludeThisKeyword && node.expression.kind === SyntaxKind.ThisKeyword || isIdentifier(node.name) && isBindableStaticNameExpression(node.expression, /*excludeThisKeyword*/ true))
        || isBindableStaticElementAccessExpression(node, excludeThisKeyword);
}

/**
 * Any series of property and element accesses, ending in a literal element access
 *
 * @internal
 */
export function isBindableStaticElementAccessExpression(node: Node, excludeThisKeyword?: boolean): node is BindableStaticElementAccessExpression {
    return isLiteralLikeElementAccess(node)
        && ((!excludeThisKeyword && node.expression.kind === SyntaxKind.ThisKeyword) ||
            isEntityNameExpression(node.expression) ||
            isBindableStaticAccessExpression(node.expression, /*excludeThisKeyword*/ true));
}

/** @internal */
export function isBindableStaticNameExpression(node: Node, excludeThisKeyword?: boolean): node is BindableStaticNameExpression {
    return isEntityNameExpression(node) || isBindableStaticAccessExpression(node, excludeThisKeyword);
}

/** @internal */
export function getNameOrArgument(expr: PropertyAccessExpression | LiteralLikeElementAccessExpression) {
    if (isPropertyAccessExpression(expr)) {
        return expr.name;
    }
    return expr.argumentExpression;
}

function getAssignmentDeclarationKindWorker(expr: BinaryExpression | CallExpression): AssignmentDeclarationKind {
    if (isCallExpression(expr)) {
        if (!isBindableObjectDefinePropertyCall(expr)) {
            return AssignmentDeclarationKind.None;
        }
        const entityName = expr.arguments[0];
        if (isExportsIdentifier(entityName) || isModuleExportsAccessExpression(entityName)) {
            return AssignmentDeclarationKind.ObjectDefinePropertyExports;
        }
        if (isBindableStaticAccessExpression(entityName) && getElementOrPropertyAccessName(entityName) === "prototype") {
            return AssignmentDeclarationKind.ObjectDefinePrototypeProperty;
        }
        return AssignmentDeclarationKind.ObjectDefinePropertyValue;
    }
    if (expr.operatorToken.kind !== SyntaxKind.EqualsToken || !isAccessExpression(expr.left) || isVoidZero(getRightMostAssignedExpression(expr))) {
        return AssignmentDeclarationKind.None;
    }
    if (isBindableStaticNameExpression(expr.left.expression, /*excludeThisKeyword*/ true) && getElementOrPropertyAccessName(expr.left) === "prototype" && isObjectLiteralExpression(getInitializerOfBinaryExpression(expr))) {
        // F.prototype = { ... }
        return AssignmentDeclarationKind.Prototype;
    }
    return getAssignmentDeclarationPropertyAccessKind(expr.left);
}

function isVoidZero(node: Node) {
    return isVoidExpression(node) && isNumericLiteral(node.expression) && node.expression.text === "0";
}

/**
 * Does not handle signed numeric names like `a[+0]` - handling those would require handling prefix unary expressions
 * throughout late binding handling as well, which is awkward (but ultimately probably doable if there is demand)
 *
 * @internal
 */
export function getElementOrPropertyAccessArgumentExpressionOrName(node: AccessExpression): Identifier | PrivateIdentifier | StringLiteralLike | NumericLiteral | ElementAccessExpression | undefined {
    if (isPropertyAccessExpression(node)) {
        return node.name;
    }
    const arg = skipParentheses(node.argumentExpression);
    if (isNumericLiteral(arg) || isStringLiteralLike(arg)) {
        return arg;
    }
    return node;
}

/** @internal */
export function getElementOrPropertyAccessName(node: LiteralLikeElementAccessExpression | PropertyAccessExpression): __String;
/** @internal */
export function getElementOrPropertyAccessName(node: AccessExpression): __String | undefined;
/** @internal */
export function getElementOrPropertyAccessName(node: AccessExpression): __String | undefined {
    const name = getElementOrPropertyAccessArgumentExpressionOrName(node);
    if (name) {
        if (isIdentifier(name)) {
            return name.escapedText;
        }
        if (isStringLiteralLike(name) || isNumericLiteral(name)) {
            return escapeLeadingUnderscores(name.text);
        }
    }
    return undefined;
}

/** @internal */
export function getAssignmentDeclarationPropertyAccessKind(lhs: AccessExpression): AssignmentDeclarationKind {
    if (lhs.expression.kind === SyntaxKind.ThisKeyword) {
        return AssignmentDeclarationKind.ThisProperty;
    }
    else if (isModuleExportsAccessExpression(lhs)) {
        // module.exports = expr
        return AssignmentDeclarationKind.ModuleExports;
    }
    else if (isBindableStaticNameExpression(lhs.expression, /*excludeThisKeyword*/ true)) {
        if (isPrototypeAccess(lhs.expression)) {
            // F.G....prototype.x = expr
            return AssignmentDeclarationKind.PrototypeProperty;
        }

        let nextToLast = lhs;
        while (!isIdentifier(nextToLast.expression)) {
            nextToLast = nextToLast.expression as Exclude<BindableStaticNameExpression, Identifier>;
        }
        const id = nextToLast.expression;
        if (
            (id.escapedText === "exports" ||
                id.escapedText === "module" && getElementOrPropertyAccessName(nextToLast) === "exports") &&
            // ExportsProperty does not support binding with computed names
            isBindableStaticAccessExpression(lhs)
        ) {
            // exports.name = expr OR module.exports.name = expr OR exports["name"] = expr ...
            return AssignmentDeclarationKind.ExportsProperty;
        }
        if (isBindableStaticNameExpression(lhs, /*excludeThisKeyword*/ true) || (isElementAccessExpression(lhs) && isDynamicName(lhs))) {
            // F.G...x = expr
            return AssignmentDeclarationKind.Property;
        }
    }

    return AssignmentDeclarationKind.None;
}

/** @internal */
export function getInitializerOfBinaryExpression(expr: BinaryExpression) {
    while (isBinaryExpression(expr.right)) {
        expr = expr.right;
    }
    return expr.right;
}

/** @internal */
export interface PrototypePropertyAssignment extends AssignmentExpression<EqualsToken> {
    _prototypePropertyAssignmentBrand: any;
    readonly left: AccessExpression;
}

/** @internal */
export function isPrototypePropertyAssignment(node: Node): node is PrototypePropertyAssignment {
    return isBinaryExpression(node) && getAssignmentDeclarationKind(node) === AssignmentDeclarationKind.PrototypeProperty;
}

/** @internal */
export function isSpecialPropertyDeclaration(expr: PropertyAccessExpression | ElementAccessExpression): expr is PropertyAccessExpression | LiteralLikeElementAccessExpression {
    return isInJSFile(expr) &&
        expr.parent && expr.parent.kind === SyntaxKind.ExpressionStatement &&
        (!isElementAccessExpression(expr) || isLiteralLikeElementAccess(expr)) &&
        !!getJSDocTypeTag(expr.parent);
}

/** @internal */
export function setValueDeclaration(symbol: Symbol, node: Declaration): void {
    const { valueDeclaration } = symbol;
    if (
        !valueDeclaration ||
        !(node.flags & NodeFlags.Ambient && !isInJSFile(node) && !(valueDeclaration.flags & NodeFlags.Ambient)) &&
            (isAssignmentDeclaration(valueDeclaration) && !isAssignmentDeclaration(node)) ||
        (valueDeclaration.kind !== node.kind && isEffectiveModuleDeclaration(valueDeclaration))
    ) {
        // other kinds of value declarations take precedence over modules and assignment declarations
        symbol.valueDeclaration = node;
    }
}

/** @internal */
export function isFunctionSymbol(symbol: Symbol | undefined) {
    if (!symbol || !symbol.valueDeclaration) {
        return false;
    }
    const decl = symbol.valueDeclaration;
    return decl.kind === SyntaxKind.FunctionDeclaration || isVariableDeclaration(decl) && decl.initializer && isFunctionLike(decl.initializer);
}

/** @internal */
export function tryGetModuleSpecifierFromDeclaration(node: AnyImportOrBareOrAccessedRequire | AliasDeclarationNode | ExportDeclaration | ImportTypeNode): StringLiteralLike | undefined {
    switch (node.kind) {
        case SyntaxKind.VariableDeclaration:
        case SyntaxKind.BindingElement:
            return findAncestor(node.initializer, (node): node is RequireOrImportCall => isRequireCall(node, /*requireStringLiteralLikeArgument*/ true))?.arguments[0];
        case SyntaxKind.ImportDeclaration:
        case SyntaxKind.ExportDeclaration:
            return tryCast(node.moduleSpecifier, isStringLiteralLike);
        case SyntaxKind.ImportEqualsDeclaration:
            return tryCast(tryCast(node.moduleReference, isExternalModuleReference)?.expression, isStringLiteralLike);
        case SyntaxKind.ImportClause:
        case SyntaxKind.NamespaceExport:
            return tryCast(node.parent.moduleSpecifier, isStringLiteralLike);
        case SyntaxKind.NamespaceImport:
        case SyntaxKind.ExportSpecifier:
            return tryCast(node.parent.parent.moduleSpecifier, isStringLiteralLike);
        case SyntaxKind.ImportSpecifier:
            return tryCast(node.parent.parent.parent.moduleSpecifier, isStringLiteralLike);
        case SyntaxKind.ImportType:
            return isLiteralImportTypeNode(node) ? node.argument.literal : undefined;
        default:
            Debug.assertNever(node);
    }
}

/** @internal */
export function importFromModuleSpecifier(node: StringLiteralLike): AnyValidImportOrReExport {
    return tryGetImportFromModuleSpecifier(node) || Debug.failBadSyntaxKind(node.parent);
}

/** @internal */
export function tryGetImportFromModuleSpecifier(node: StringLiteralLike): AnyValidImportOrReExport | undefined {
    switch (node.parent.kind) {
        case SyntaxKind.ImportDeclaration:
        case SyntaxKind.ExportDeclaration:
            return node.parent as AnyValidImportOrReExport;
        case SyntaxKind.ExternalModuleReference:
            return (node.parent as ExternalModuleReference).parent as AnyValidImportOrReExport;
        case SyntaxKind.CallExpression:
            return isImportCall(node.parent) || isRequireCall(node.parent, /*requireStringLiteralLikeArgument*/ false) ? node.parent as RequireOrImportCall : undefined;
        case SyntaxKind.LiteralType:
            Debug.assert(isStringLiteral(node));
            return tryCast(node.parent.parent, isImportTypeNode) as ValidImportTypeNode | undefined;
        default:
            return undefined;
    }
}

/** @internal */
export function getExternalModuleName(node: AnyImportOrReExport | ImportTypeNode | ImportCall | ModuleDeclaration): Expression | undefined {
    switch (node.kind) {
        case SyntaxKind.ImportDeclaration:
        case SyntaxKind.ExportDeclaration:
            return node.moduleSpecifier;
        case SyntaxKind.ImportEqualsDeclaration:
            return node.moduleReference.kind === SyntaxKind.ExternalModuleReference ? node.moduleReference.expression : undefined;
        case SyntaxKind.ImportType:
            return isLiteralImportTypeNode(node) ? node.argument.literal : undefined;
        case SyntaxKind.CallExpression:
            return node.arguments[0];
        case SyntaxKind.ModuleDeclaration:
            return node.name.kind === SyntaxKind.StringLiteral ? node.name : undefined;
        default:
            return Debug.assertNever(node);
    }
}

/** @internal */
export function getNamespaceDeclarationNode(node: ImportDeclaration | ImportEqualsDeclaration | ExportDeclaration): ImportEqualsDeclaration | NamespaceImport | NamespaceExport | undefined {
    switch (node.kind) {
        case SyntaxKind.ImportDeclaration:
            return node.importClause && tryCast(node.importClause.namedBindings, isNamespaceImport);
        case SyntaxKind.ImportEqualsDeclaration:
            return node;
        case SyntaxKind.ExportDeclaration:
            return node.exportClause && tryCast(node.exportClause, isNamespaceExport);
        default:
            return Debug.assertNever(node);
    }
}

/** @internal */
export function isDefaultImport(node: ImportDeclaration | ImportEqualsDeclaration | ExportDeclaration): boolean {
    return node.kind === SyntaxKind.ImportDeclaration && !!node.importClause && !!node.importClause.name;
}

/** @internal */
export function forEachImportClauseDeclaration<T>(node: ImportClause, action: (declaration: ImportClause | NamespaceImport | ImportSpecifier) => T | undefined): T | undefined {
    if (node.name) {
        const result = action(node);
        if (result) return result;
    }
    if (node.namedBindings) {
        const result = isNamespaceImport(node.namedBindings)
            ? action(node.namedBindings)
            : forEach(node.namedBindings.elements, action);
        if (result) return result;
    }
}

/** @internal */
export function hasQuestionToken(node: Node) {
    if (node) {
        switch (node.kind) {
            case SyntaxKind.Parameter:
            case SyntaxKind.MethodDeclaration:
            case SyntaxKind.MethodSignature:
            case SyntaxKind.ShorthandPropertyAssignment:
            case SyntaxKind.PropertyAssignment:
            case SyntaxKind.PropertyDeclaration:
            case SyntaxKind.PropertySignature:
                return (node as ParameterDeclaration | MethodDeclaration | PropertyDeclaration).questionToken !== undefined;
        }
    }

    return false;
}

/** @internal */
export function isJSDocConstructSignature(node: Node) {
    const param = isJSDocFunctionType(node) ? firstOrUndefined(node.parameters) : undefined;
    const name = tryCast(param && param.name, isIdentifier);
    return !!name && name.escapedText === "new";
}

/** @internal */
export function isJSDocTypeAlias(node: Node): node is JSDocTypedefTag | JSDocCallbackTag | JSDocEnumTag {
    return node.kind === SyntaxKind.JSDocTypedefTag || node.kind === SyntaxKind.JSDocCallbackTag || node.kind === SyntaxKind.JSDocEnumTag;
}

/** @internal */
export function isTypeAlias(node: Node): node is JSDocTypedefTag | JSDocCallbackTag | JSDocEnumTag | TypeAliasDeclaration {
    return isJSDocTypeAlias(node) || isTypeAliasDeclaration(node);
}

function getSourceOfAssignment(node: Node): Node | undefined {
    return isExpressionStatement(node) &&
            isBinaryExpression(node.expression) &&
            node.expression.operatorToken.kind === SyntaxKind.EqualsToken
        ? getRightMostAssignedExpression(node.expression)
        : undefined;
}

function getSourceOfDefaultedAssignment(node: Node): Node | undefined {
    return isExpressionStatement(node) &&
            isBinaryExpression(node.expression) &&
            getAssignmentDeclarationKind(node.expression) !== AssignmentDeclarationKind.None &&
            isBinaryExpression(node.expression.right) &&
            (node.expression.right.operatorToken.kind === SyntaxKind.BarBarToken || node.expression.right.operatorToken.kind === SyntaxKind.QuestionQuestionToken)
        ? node.expression.right.right
        : undefined;
}

/** @internal */
export function getSingleInitializerOfVariableStatementOrPropertyDeclaration(node: Node): Expression | undefined {
    switch (node.kind) {
        case SyntaxKind.VariableStatement:
            const v = getSingleVariableOfVariableStatement(node);
            return v && v.initializer;
        case SyntaxKind.PropertyDeclaration:
            return (node as PropertyDeclaration).initializer;
        case SyntaxKind.PropertyAssignment:
            return (node as PropertyAssignment).initializer;
    }
}

/** @internal */
export function getSingleVariableOfVariableStatement(node: Node): VariableDeclaration | undefined {
    return isVariableStatement(node) ? firstOrUndefined(node.declarationList.declarations) : undefined;
}

function getNestedModuleDeclaration(node: Node): Node | undefined {
    return isModuleDeclaration(node) &&
            node.body &&
            node.body.kind === SyntaxKind.ModuleDeclaration
        ? node.body
        : undefined;
}

/** @internal */
export function canHaveFlowNode(node: Node): node is HasFlowNode {
    if (node.kind >= SyntaxKind.FirstStatement && node.kind <= SyntaxKind.LastStatement) {
        return true;
    }

    switch (node.kind) {
        case SyntaxKind.Identifier:
        case SyntaxKind.ThisKeyword:
        case SyntaxKind.SuperKeyword:
        case SyntaxKind.QualifiedName:
        case SyntaxKind.MetaProperty:
        case SyntaxKind.ElementAccessExpression:
        case SyntaxKind.PropertyAccessExpression:
        case SyntaxKind.BindingElement:
        case SyntaxKind.FunctionExpression:
        case SyntaxKind.ArrowFunction:
        case SyntaxKind.MethodDeclaration:
        case SyntaxKind.GetAccessor:
        case SyntaxKind.SetAccessor:
            return true;
        default:
            return false;
    }
}

/** @internal */
export function canHaveJSDoc(node: Node): node is HasJSDoc {
    switch (node.kind) {
        case SyntaxKind.ArrowFunction:
        case SyntaxKind.BinaryExpression:
        case SyntaxKind.Block:
        case SyntaxKind.BreakStatement:
        case SyntaxKind.CallSignature:
        case SyntaxKind.CaseClause:
        case SyntaxKind.ClassDeclaration:
        case SyntaxKind.ClassExpression:
        case SyntaxKind.ClassStaticBlockDeclaration:
        case SyntaxKind.Constructor:
        case SyntaxKind.ConstructorType:
        case SyntaxKind.ConstructSignature:
        case SyntaxKind.ContinueStatement:
        case SyntaxKind.DebuggerStatement:
        case SyntaxKind.DoStatement:
        case SyntaxKind.ElementAccessExpression:
        case SyntaxKind.EmptyStatement:
        case SyntaxKind.EndOfFileToken:
        case SyntaxKind.EnumDeclaration:
        case SyntaxKind.EnumMember:
        case SyntaxKind.ExportAssignment:
        case SyntaxKind.ExportDeclaration:
        case SyntaxKind.ExportSpecifier:
        case SyntaxKind.ExpressionStatement:
        case SyntaxKind.ForInStatement:
        case SyntaxKind.ForOfStatement:
        case SyntaxKind.ForStatement:
        case SyntaxKind.FunctionDeclaration:
        case SyntaxKind.FunctionExpression:
        case SyntaxKind.FunctionType:
        case SyntaxKind.GetAccessor:
        case SyntaxKind.Identifier:
        case SyntaxKind.IfStatement:
        case SyntaxKind.ImportDeclaration:
        case SyntaxKind.ImportEqualsDeclaration:
        case SyntaxKind.IndexSignature:
        case SyntaxKind.InterfaceDeclaration:
        case SyntaxKind.JSDocFunctionType:
        case SyntaxKind.JSDocSignature:
        case SyntaxKind.LabeledStatement:
        case SyntaxKind.MethodDeclaration:
        case SyntaxKind.MethodSignature:
        case SyntaxKind.ModuleDeclaration:
        case SyntaxKind.NamedTupleMember:
        case SyntaxKind.NamespaceExportDeclaration:
        case SyntaxKind.ObjectLiteralExpression:
        case SyntaxKind.Parameter:
        case SyntaxKind.ParenthesizedExpression:
        case SyntaxKind.PropertyAccessExpression:
        case SyntaxKind.PropertyAssignment:
        case SyntaxKind.PropertyDeclaration:
        case SyntaxKind.PropertySignature:
        case SyntaxKind.ReturnStatement:
        case SyntaxKind.SemicolonClassElement:
        case SyntaxKind.SetAccessor:
        case SyntaxKind.ShorthandPropertyAssignment:
        case SyntaxKind.SpreadAssignment:
        case SyntaxKind.SwitchStatement:
        case SyntaxKind.ThrowStatement:
        case SyntaxKind.TryStatement:
        case SyntaxKind.TypeAliasDeclaration:
        case SyntaxKind.TypeParameter:
        case SyntaxKind.VariableDeclaration:
        case SyntaxKind.VariableStatement:
        case SyntaxKind.WhileStatement:
        case SyntaxKind.WithStatement:
            return true;
        default:
            return false;
    }
}

/**
 * This function checks multiple locations for JSDoc comments that apply to a host node.
 * At each location, the whole comment may apply to the node, or only a specific tag in
 * the comment. In the first case, location adds the entire {@link JSDoc} object. In the
 * second case, it adds the applicable {@link JSDocTag}.
 *
 * For example, a JSDoc comment before a parameter adds the entire {@link JSDoc}. But a
 * `@param` tag on the parent function only adds the {@link JSDocTag} for the `@param`.
 *
 * ```ts
 * /** JSDoc will be returned for `a` *\/
 * const a = 0
 * /**
 *  * Entire JSDoc will be returned for `b`
 *  * @param c JSDocTag will be returned for `c`
 *  *\/
 * function b(/** JSDoc will be returned for `c` *\/ c) {}
 * ```
 */
export function getJSDocCommentsAndTags(hostNode: Node): readonly (JSDoc | JSDocTag)[];
/** @internal separate signature so that stripInternal can remove noCache from the public API */
// eslint-disable-next-line @typescript-eslint/unified-signatures
export function getJSDocCommentsAndTags(hostNode: Node, noCache?: boolean): readonly (JSDoc | JSDocTag)[];
export function getJSDocCommentsAndTags(hostNode: Node, noCache?: boolean): readonly (JSDoc | JSDocTag)[] {
    let result: (JSDoc | JSDocTag)[] | undefined;
    // Pull parameter comments from declaring function as well
    if (isVariableLike(hostNode) && hasInitializer(hostNode) && hasJSDocNodes(hostNode.initializer!)) {
        result = addRange(result, filterOwnedJSDocTags(hostNode, hostNode.initializer.jsDoc!));
    }

    let node: Node | undefined = hostNode;
    while (node && node.parent) {
        if (hasJSDocNodes(node)) {
            result = addRange(result, filterOwnedJSDocTags(hostNode, node.jsDoc!));
        }

        if (node.kind === SyntaxKind.Parameter) {
            result = addRange(result, (noCache ? getJSDocParameterTagsNoCache : getJSDocParameterTags)(node as ParameterDeclaration));
            break;
        }
        if (node.kind === SyntaxKind.TypeParameter) {
            result = addRange(result, (noCache ? getJSDocTypeParameterTagsNoCache : getJSDocTypeParameterTags)(node as TypeParameterDeclaration));
            break;
        }
        node = getNextJSDocCommentLocation(node);
    }
    return result || emptyArray;
}

function filterOwnedJSDocTags(hostNode: Node, comments: JSDocArray) {
    const lastJsDoc = last(comments);
    return flatMap<JSDoc, JSDoc | JSDocTag>(comments, jsDoc => {
        if (jsDoc === lastJsDoc) {
            const ownedTags = filter(jsDoc.tags, tag => ownsJSDocTag(hostNode, tag));
            return jsDoc.tags === ownedTags ? [jsDoc] : ownedTags;
        }
        else {
            return filter(jsDoc.tags, isJSDocOverloadTag);
        }
    });
}

/**
 * Determines whether a host node owns a jsDoc tag. A `@type`/`@satisfies` tag attached to a
 * a ParenthesizedExpression belongs only to the ParenthesizedExpression.
 */
function ownsJSDocTag(hostNode: Node, tag: JSDocTag) {
    return !(isJSDocTypeTag(tag) || isJSDocSatisfiesTag(tag))
        || !tag.parent
        || !isJSDoc(tag.parent)
        || !isParenthesizedExpression(tag.parent.parent)
        || tag.parent.parent === hostNode;
}

/** @internal */
export function getNextJSDocCommentLocation(node: Node) {
    const parent = node.parent;
    if (
        parent.kind === SyntaxKind.PropertyAssignment ||
        parent.kind === SyntaxKind.ExportAssignment ||
        parent.kind === SyntaxKind.PropertyDeclaration ||
        parent.kind === SyntaxKind.ExpressionStatement && node.kind === SyntaxKind.PropertyAccessExpression ||
        parent.kind === SyntaxKind.ReturnStatement ||
        getNestedModuleDeclaration(parent) ||
        isAssignmentExpression(node)
    ) {
        return parent;
    }
    // Try to recognize this pattern when node is initializer of variable declaration and JSDoc comments are on containing variable statement.
    // /**
    //   * @param {number} name
    //   * @returns {number}
    //   */
    // var x = function(name) { return name.length; }
    else if (
        parent.parent &&
        (getSingleVariableOfVariableStatement(parent.parent) === node || isAssignmentExpression(parent))
    ) {
        return parent.parent;
    }
    else if (
        parent.parent && parent.parent.parent &&
        (getSingleVariableOfVariableStatement(parent.parent.parent) ||
            getSingleInitializerOfVariableStatementOrPropertyDeclaration(parent.parent.parent) === node ||
            getSourceOfDefaultedAssignment(parent.parent.parent))
    ) {
        return parent.parent.parent;
    }
}

/**
 * Does the opposite of `getJSDocParameterTags`: given a JSDoc parameter, finds the parameter corresponding to it.
 *
 * @internal
 */
export function getParameterSymbolFromJSDoc(node: JSDocParameterTag): Symbol | undefined {
    if (node.symbol) {
        return node.symbol;
    }
    if (!isIdentifier(node.name)) {
        return undefined;
    }
    const name = node.name.escapedText;
    const decl = getHostSignatureFromJSDoc(node);
    if (!decl) {
        return undefined;
    }
    const parameter = find(decl.parameters, p => p.name.kind === SyntaxKind.Identifier && p.name.escapedText === name);
    return parameter && parameter.symbol;
}

/** @internal */
export function getEffectiveContainerForJSDocTemplateTag(node: JSDocTemplateTag) {
    if (isJSDoc(node.parent) && node.parent.tags) {
        // A @template tag belongs to any @typedef, @callback, or @enum tags in the same comment block, if they exist.
        const typeAlias = find(node.parent.tags, isJSDocTypeAlias);
        if (typeAlias) {
            return typeAlias;
        }
    }
    // otherwise it belongs to the host it annotates
    return getHostSignatureFromJSDoc(node);
}

/** @internal */
export function getJSDocOverloadTags(node: Node): readonly JSDocOverloadTag[] {
    return getAllJSDocTags(node, isJSDocOverloadTag);
}

/** @internal */
export function getHostSignatureFromJSDoc(node: Node): SignatureDeclaration | undefined {
    const host = getEffectiveJSDocHost(node);
    if (host) {
        return isPropertySignature(host) && host.type && isFunctionLike(host.type) ? host.type :
            isFunctionLike(host) ? host : undefined;
    }
    return undefined;
}

/** @internal */
export function getEffectiveJSDocHost(node: Node): Node | undefined {
    const host = getJSDocHost(node);
    if (host) {
        return getSourceOfDefaultedAssignment(host)
            || getSourceOfAssignment(host)
            || getSingleInitializerOfVariableStatementOrPropertyDeclaration(host)
            || getSingleVariableOfVariableStatement(host)
            || getNestedModuleDeclaration(host)
            || host;
    }
}

/**
 * Use getEffectiveJSDocHost if you additionally need to look for jsdoc on parent nodes, like assignments.
 *
 * @internal
 */
export function getJSDocHost(node: Node): HasJSDoc | undefined {
    const jsDoc = getJSDocRoot(node);
    if (!jsDoc) {
        return undefined;
    }

    const host = jsDoc.parent;
    if (host && host.jsDoc && jsDoc === lastOrUndefined(host.jsDoc)) {
        return host;
    }
}

/** @internal */
export function getJSDocRoot(node: Node): JSDoc | undefined {
    return findAncestor(node.parent, isJSDoc);
}

/** @internal */
export function getTypeParameterFromJsDoc(node: TypeParameterDeclaration & { parent: JSDocTemplateTag; }): TypeParameterDeclaration | undefined {
    const name = node.name.escapedText;
    const { typeParameters } = node.parent.parent.parent as SignatureDeclaration | InterfaceDeclaration | ClassDeclaration;
    return typeParameters && find(typeParameters, p => p.name.escapedText === name);
}

/** @internal */
export function hasTypeArguments(node: Node): node is HasTypeArguments {
    return !!(node as HasTypeArguments).typeArguments;
}

/** @internal */
export const enum AssignmentKind {
    None,
    Definite,
    Compound,
}

type AssignmentTarget =
    | BinaryExpression
    | PrefixUnaryExpression
    | PostfixUnaryExpression
    | ForInOrOfStatement;

function getAssignmentTarget(node: Node): AssignmentTarget | undefined {
    let parent = node.parent;
    while (true) {
        switch (parent.kind) {
            case SyntaxKind.BinaryExpression:
                const binaryExpression = parent as BinaryExpression;
                const binaryOperator = binaryExpression.operatorToken.kind;
                return isAssignmentOperator(binaryOperator) && binaryExpression.left === node ? binaryExpression : undefined;
            case SyntaxKind.PrefixUnaryExpression:
            case SyntaxKind.PostfixUnaryExpression:
                const unaryExpression = parent as PrefixUnaryExpression | PostfixUnaryExpression;
                const unaryOperator = unaryExpression.operator;
                return unaryOperator === SyntaxKind.PlusPlusToken || unaryOperator === SyntaxKind.MinusMinusToken ? unaryExpression : undefined;
            case SyntaxKind.ForInStatement:
            case SyntaxKind.ForOfStatement:
                const forInOrOfStatement = parent as ForInOrOfStatement;
                return forInOrOfStatement.initializer === node ? forInOrOfStatement : undefined;
            case SyntaxKind.ParenthesizedExpression:
            case SyntaxKind.ArrayLiteralExpression:
            case SyntaxKind.SpreadElement:
            case SyntaxKind.NonNullExpression:
                node = parent;
                break;
            case SyntaxKind.SpreadAssignment:
                node = parent.parent;
                break;
            case SyntaxKind.ShorthandPropertyAssignment:
                if ((parent as ShorthandPropertyAssignment).name !== node) {
                    return undefined;
                }
                node = parent.parent;
                break;
            case SyntaxKind.PropertyAssignment:
                if ((parent as PropertyAssignment).name === node) {
                    return undefined;
                }
                node = parent.parent;
                break;
            default:
                return undefined;
        }
        parent = node.parent;
    }
}

/** @internal */
export function getAssignmentTargetKind(node: Node): AssignmentKind {
    const target = getAssignmentTarget(node);
    if (!target) {
        return AssignmentKind.None;
    }
    switch (target.kind) {
        case SyntaxKind.BinaryExpression:
            const binaryOperator = target.operatorToken.kind;
            return binaryOperator === SyntaxKind.EqualsToken || isLogicalOrCoalescingAssignmentOperator(binaryOperator) ?
                AssignmentKind.Definite :
                AssignmentKind.Compound;
        case SyntaxKind.PrefixUnaryExpression:
        case SyntaxKind.PostfixUnaryExpression:
            return AssignmentKind.Compound;
        case SyntaxKind.ForInStatement:
        case SyntaxKind.ForOfStatement:
            return AssignmentKind.Definite;
    }
}

// A node is an assignment target if it is on the left hand side of an '=' token, if it is parented by a property
// assignment in an object literal that is an assignment target, or if it is parented by an array literal that is
// an assignment target. Examples include 'a = xxx', '{ p: a } = xxx', '[{ a }] = xxx'.
// (Note that `p` is not a target in the above examples, only `a`.)
/** @internal */
export function isAssignmentTarget(node: Node): boolean {
    return !!getAssignmentTarget(node);
}

function isCompoundLikeAssignment(assignment: AssignmentExpression<EqualsToken>): boolean {
    const right = skipParentheses(assignment.right);
    return right.kind === SyntaxKind.BinaryExpression && isShiftOperatorOrHigher((right as BinaryExpression).operatorToken.kind);
}

/** @internal */
export function isInCompoundLikeAssignment(node: Node): boolean {
    const target = getAssignmentTarget(node);
    return !!target && isAssignmentExpression(target, /*excludeCompoundAssignment*/ true) && isCompoundLikeAssignment(target);
}

/** @internal */
export type NodeWithPossibleHoistedDeclaration =
    | Block
    | VariableStatement
    | WithStatement
    | IfStatement
    | SwitchStatement
    | CaseBlock
    | CaseClause
    | DefaultClause
    | LabeledStatement
    | ForStatement
    | ForInOrOfStatement
    | DoStatement
    | WhileStatement
    | TryStatement
    | CatchClause;

/**
 * Indicates whether a node could contain a `var` VariableDeclarationList that contributes to
 * the same `var` declaration scope as the node's parent.
 *
 * @internal
 */
export function isNodeWithPossibleHoistedDeclaration(node: Node): node is NodeWithPossibleHoistedDeclaration {
    switch (node.kind) {
        case SyntaxKind.Block:
        case SyntaxKind.VariableStatement:
        case SyntaxKind.WithStatement:
        case SyntaxKind.IfStatement:
        case SyntaxKind.SwitchStatement:
        case SyntaxKind.CaseBlock:
        case SyntaxKind.CaseClause:
        case SyntaxKind.DefaultClause:
        case SyntaxKind.LabeledStatement:
        case SyntaxKind.ForStatement:
        case SyntaxKind.ForInStatement:
        case SyntaxKind.ForOfStatement:
        case SyntaxKind.DoStatement:
        case SyntaxKind.WhileStatement:
        case SyntaxKind.TryStatement:
        case SyntaxKind.CatchClause:
            return true;
    }
    return false;
}

/** @internal */
export type ValueSignatureDeclaration =
    | FunctionDeclaration
    | MethodDeclaration
    | ConstructorDeclaration
    | AccessorDeclaration
    | FunctionExpression
    | ArrowFunction;

/** @internal */
export function isValueSignatureDeclaration(node: Node): node is ValueSignatureDeclaration {
    return isFunctionExpression(node) || isArrowFunction(node) || isMethodOrAccessor(node) || isFunctionDeclaration(node) || isConstructorDeclaration(node);
}

function walkUp(node: Node, kind: SyntaxKind) {
    while (node && node.kind === kind) {
        node = node.parent;
    }
    return node;
}

/** @internal */
export function walkUpParenthesizedTypes(node: Node) {
    return walkUp(node, SyntaxKind.ParenthesizedType);
}

/** @internal */
export function walkUpParenthesizedExpressions(node: Node) {
    return walkUp(node, SyntaxKind.ParenthesizedExpression);
}

/**
 * Walks up parenthesized types.
 * It returns both the outermost parenthesized type and its parent.
 * If given node is not a parenthesiezd type, undefined is return as the former.
 *
 * @internal
 */
export function walkUpParenthesizedTypesAndGetParentAndChild(node: Node): [ParenthesizedTypeNode | undefined, Node] {
    let child: ParenthesizedTypeNode | undefined;
    while (node && node.kind === SyntaxKind.ParenthesizedType) {
        child = node as ParenthesizedTypeNode;
        node = node.parent;
    }
    return [child, node];
}

/** @internal */
export function skipTypeParentheses(node: TypeNode): TypeNode {
    while (isParenthesizedTypeNode(node)) node = node.type;
    return node;
}

/** @internal */
export function skipParentheses(node: Expression, excludeJSDocTypeAssertions?: boolean): Expression;
/** @internal */
export function skipParentheses(node: Node, excludeJSDocTypeAssertions?: boolean): Node;
/** @internal */
export function skipParentheses(node: Node, excludeJSDocTypeAssertions?: boolean): Node {
    const flags = excludeJSDocTypeAssertions ?
        OuterExpressionKinds.Parentheses | OuterExpressionKinds.ExcludeJSDocTypeAssertion :
        OuterExpressionKinds.Parentheses;
    return skipOuterExpressions(node, flags);
}

// a node is delete target iff. it is PropertyAccessExpression/ElementAccessExpression with parentheses skipped
/** @internal */
export function isDeleteTarget(node: Node): boolean {
    if (node.kind !== SyntaxKind.PropertyAccessExpression && node.kind !== SyntaxKind.ElementAccessExpression) {
        return false;
    }
    node = walkUpParenthesizedExpressions(node.parent);
    return node && node.kind === SyntaxKind.DeleteExpression;
}

/** @internal */
export function isNodeDescendantOf(node: Node, ancestor: Node | undefined): boolean {
    while (node) {
        if (node === ancestor) return true;
        node = node.parent;
    }
    return false;
}

// True if `name` is the name of a declaration node
/** @internal */
export function isDeclarationName(name: Node): boolean {
    return !isSourceFile(name) && !isBindingPattern(name) && isDeclaration(name.parent) && name.parent.name === name;
}

// See GH#16030
/** @internal */
export function getDeclarationFromName(name: Node): Declaration | undefined {
    const parent = name.parent;
    switch (name.kind) {
        case SyntaxKind.StringLiteral:
        case SyntaxKind.NoSubstitutionTemplateLiteral:
        case SyntaxKind.NumericLiteral:
            if (isComputedPropertyName(parent)) return parent.parent;
            // falls through
        case SyntaxKind.Identifier:
            if (isDeclaration(parent)) {
                return parent.name === name ? parent : undefined;
            }
            else if (isQualifiedName(parent)) {
                const tag = parent.parent;
                return isJSDocParameterTag(tag) && tag.name === parent ? tag : undefined;
            }
            else {
                const binExp = parent.parent;
                return isBinaryExpression(binExp) &&
                        getAssignmentDeclarationKind(binExp) !== AssignmentDeclarationKind.None &&
                        ((binExp.left as BindableStaticNameExpression).symbol || binExp.symbol) &&
                        getNameOfDeclaration(binExp) === name
                    ? binExp
                    : undefined;
            }
        case SyntaxKind.PrivateIdentifier:
            return isDeclaration(parent) && parent.name === name ? parent : undefined;
        default:
            return undefined;
    }
}

/** @internal */
export function isLiteralComputedPropertyDeclarationName(node: Node) {
    return isStringOrNumericLiteralLike(node) &&
        node.parent.kind === SyntaxKind.ComputedPropertyName &&
        isDeclaration(node.parent.parent);
}

// Return true if the given identifier is classified as an IdentifierName
/** @internal */
export function isIdentifierName(node: Identifier): boolean {
    const parent = node.parent;
    switch (parent.kind) {
        case SyntaxKind.PropertyDeclaration:
        case SyntaxKind.PropertySignature:
        case SyntaxKind.MethodDeclaration:
        case SyntaxKind.MethodSignature:
        case SyntaxKind.GetAccessor:
        case SyntaxKind.SetAccessor:
        case SyntaxKind.EnumMember:
        case SyntaxKind.PropertyAssignment:
        case SyntaxKind.PropertyAccessExpression:
            // Name in member declaration or property name in property access
            return (parent as NamedDeclaration | PropertyAccessExpression).name === node;
        case SyntaxKind.QualifiedName:
            // Name on right hand side of dot in a type query or type reference
            return (parent as QualifiedName).right === node;
        case SyntaxKind.BindingElement:
        case SyntaxKind.ImportSpecifier:
            // Property name in binding element or import specifier
            return (parent as BindingElement | ImportSpecifier).propertyName === node;
        case SyntaxKind.ExportSpecifier:
        case SyntaxKind.JsxAttribute:
        case SyntaxKind.JsxSelfClosingElement:
        case SyntaxKind.JsxOpeningElement:
        case SyntaxKind.JsxClosingElement:
            // Any name in an export specifier or JSX Attribute or Jsx Element
            return true;
    }
    return false;
}

// An alias symbol is created by one of the following declarations:
// import <symbol> = ...
// import <symbol> from ...
// import * as <symbol> from ...
// import { x as <symbol> } from ...
// export { x as <symbol> } from ...
// export * as ns <symbol> from ...
// export = <EntityNameExpression>
// export default <EntityNameExpression>
// module.exports = <EntityNameExpression>
// module.exports.x = <EntityNameExpression>
// const x = require("...")
// const { x } = require("...")
// const x = require("...").y
// const { x } = require("...").y
/** @internal */
export function isAliasSymbolDeclaration(node: Node): boolean {
    if (
        node.kind === SyntaxKind.ImportEqualsDeclaration ||
        node.kind === SyntaxKind.NamespaceExportDeclaration ||
        node.kind === SyntaxKind.ImportClause && !!(node as ImportClause).name ||
        node.kind === SyntaxKind.NamespaceImport ||
        node.kind === SyntaxKind.NamespaceExport ||
        node.kind === SyntaxKind.ImportSpecifier ||
        node.kind === SyntaxKind.ExportSpecifier ||
        node.kind === SyntaxKind.ExportAssignment && exportAssignmentIsAlias(node as ExportAssignment)
    ) {
        return true;
    }

    return isInJSFile(node) && (
        isBinaryExpression(node) && getAssignmentDeclarationKind(node) === AssignmentDeclarationKind.ModuleExports && exportAssignmentIsAlias(node) ||
        isPropertyAccessExpression(node)
            && isBinaryExpression(node.parent)
            && node.parent.left === node
            && node.parent.operatorToken.kind === SyntaxKind.EqualsToken
            && isAliasableExpression(node.parent.right)
    );
}

/** @internal */
export function getAliasDeclarationFromName(node: EntityName): Declaration | undefined {
    switch (node.parent.kind) {
        case SyntaxKind.ImportClause:
        case SyntaxKind.ImportSpecifier:
        case SyntaxKind.NamespaceImport:
        case SyntaxKind.ExportSpecifier:
        case SyntaxKind.ExportAssignment:
        case SyntaxKind.ImportEqualsDeclaration:
        case SyntaxKind.NamespaceExport:
            return node.parent as Declaration;
        case SyntaxKind.QualifiedName:
            do {
                node = node.parent as QualifiedName;
            }
            while (node.parent.kind === SyntaxKind.QualifiedName);
            return getAliasDeclarationFromName(node);
    }
}

/** @internal */
export function isAliasableExpression(e: Expression) {
    return isEntityNameExpression(e) || isClassExpression(e);
}

/** @internal */
export function exportAssignmentIsAlias(node: ExportAssignment | BinaryExpression): boolean {
    const e = getExportAssignmentExpression(node);
    return isAliasableExpression(e);
}

/** @internal */
export function getExportAssignmentExpression(node: ExportAssignment | BinaryExpression): Expression {
    return isExportAssignment(node) ? node.expression : node.right;
}

/** @internal */
export function getPropertyAssignmentAliasLikeExpression(node: PropertyAssignment | ShorthandPropertyAssignment | PropertyAccessExpression): Expression {
    return node.kind === SyntaxKind.ShorthandPropertyAssignment ? node.name : node.kind === SyntaxKind.PropertyAssignment ? node.initializer :
        (node.parent as BinaryExpression).right;
}

/** @internal */
export function getEffectiveBaseTypeNode(node: ClassLikeDeclaration | InterfaceDeclaration) {
    const baseType = getClassExtendsHeritageElement(node);
    if (baseType && isInJSFile(node)) {
        // Prefer an @augments tag because it may have type parameters.
        const tag = getJSDocAugmentsTag(node);
        if (tag) {
            return tag.class;
        }
    }
    return baseType;
}

/** @internal */
export function getClassExtendsHeritageElement(node: ClassLikeDeclaration | InterfaceDeclaration) {
    const heritageClause = getHeritageClause(node.heritageClauses, SyntaxKind.ExtendsKeyword);
    return heritageClause && heritageClause.types.length > 0 ? heritageClause.types[0] : undefined;
}

/** @internal */
export function getEffectiveImplementsTypeNodes(node: ClassLikeDeclaration): undefined | readonly ExpressionWithTypeArguments[] {
    if (isInJSFile(node)) {
        return getJSDocImplementsTags(node).map(n => n.class);
    }
    else {
        const heritageClause = getHeritageClause(node.heritageClauses, SyntaxKind.ImplementsKeyword);
        return heritageClause?.types;
    }
}

/**
 * Returns the node in an `extends` or `implements` clause of a class or interface.
 *
 * @internal
 */
export function getAllSuperTypeNodes(node: Node): readonly TypeNode[] {
    return isInterfaceDeclaration(node) ? getInterfaceBaseTypeNodes(node) || emptyArray :
        isClassLike(node) ? concatenate(singleElementArray(getEffectiveBaseTypeNode(node)), getEffectiveImplementsTypeNodes(node)) || emptyArray :
        emptyArray;
}

/** @internal */
export function getInterfaceBaseTypeNodes(node: InterfaceDeclaration) {
    const heritageClause = getHeritageClause(node.heritageClauses, SyntaxKind.ExtendsKeyword);
    return heritageClause ? heritageClause.types : undefined;
}

/** @internal */
export function getHeritageClause(clauses: NodeArray<HeritageClause> | undefined, kind: SyntaxKind) {
    if (clauses) {
        for (const clause of clauses) {
            if (clause.token === kind) {
                return clause;
            }
        }
    }

    return undefined;
}

/** @internal */
export function getAncestor(node: Node | undefined, kind: SyntaxKind): Node | undefined {
    while (node) {
        if (node.kind === kind) {
            return node;
        }
        node = node.parent;
    }
    return undefined;
}

/** @internal */
export function isKeyword(token: SyntaxKind): token is KeywordSyntaxKind {
    return SyntaxKind.FirstKeyword <= token && token <= SyntaxKind.LastKeyword;
}

/** @internal */
export function isPunctuation(token: SyntaxKind): token is PunctuationSyntaxKind {
    return SyntaxKind.FirstPunctuation <= token && token <= SyntaxKind.LastPunctuation;
}

/** @internal */
export function isKeywordOrPunctuation(token: SyntaxKind): token is PunctuationOrKeywordSyntaxKind {
    return isKeyword(token) || isPunctuation(token);
}

/** @internal */
export function isContextualKeyword(token: SyntaxKind): boolean {
    return SyntaxKind.FirstContextualKeyword <= token && token <= SyntaxKind.LastContextualKeyword;
}

/** @internal */
export function isNonContextualKeyword(token: SyntaxKind): boolean {
    return isKeyword(token) && !isContextualKeyword(token);
}

/** @internal */
export function isFutureReservedKeyword(token: SyntaxKind): boolean {
    return SyntaxKind.FirstFutureReservedWord <= token && token <= SyntaxKind.LastFutureReservedWord;
}

/** @internal */
export function isStringANonContextualKeyword(name: string) {
    const token = stringToToken(name);
    return token !== undefined && isNonContextualKeyword(token);
}

/** @internal */
export function isStringAKeyword(name: string) {
    const token = stringToToken(name);
    return token !== undefined && isKeyword(token);
}

/** @internal */
export function isIdentifierANonContextualKeyword(node: Identifier): boolean {
    const originalKeywordKind = identifierToKeywordKind(node);
    return !!originalKeywordKind && !isContextualKeyword(originalKeywordKind);
}

/** @internal */
export function isTrivia(token: SyntaxKind): token is TriviaSyntaxKind {
    return SyntaxKind.FirstTriviaToken <= token && token <= SyntaxKind.LastTriviaToken;
}

// dprint-ignore
/** @internal */
export const enum FunctionFlags {
    Normal = 0,             // Function is a normal function
    Generator = 1 << 0,     // Function is a generator function or async generator function
    Async = 1 << 1,         // Function is an async function or an async generator function
    Invalid = 1 << 2,       // Function is a signature or overload and does not have a body.
    AsyncGenerator = Async | Generator, // Function is an async generator function
}

/** @internal */
export function getFunctionFlags(node: SignatureDeclaration | undefined) {
    if (!node) {
        return FunctionFlags.Invalid;
    }

    let flags = FunctionFlags.Normal;
    switch (node.kind) {
        case SyntaxKind.FunctionDeclaration:
        case SyntaxKind.FunctionExpression:
        case SyntaxKind.MethodDeclaration:
            if (node.asteriskToken) {
                flags |= FunctionFlags.Generator;
            }
            // falls through

        case SyntaxKind.ArrowFunction:
            if (hasSyntacticModifier(node, ModifierFlags.Async)) {
                flags |= FunctionFlags.Async;
            }
            break;
    }

    if (!(node as FunctionLikeDeclaration).body) {
        flags |= FunctionFlags.Invalid;
    }

    return flags;
}

/** @internal */
export function isAsyncFunction(node: Node): boolean {
    switch (node.kind) {
        case SyntaxKind.FunctionDeclaration:
        case SyntaxKind.FunctionExpression:
        case SyntaxKind.ArrowFunction:
        case SyntaxKind.MethodDeclaration:
            return (node as FunctionLikeDeclaration).body !== undefined
                && (node as FunctionLikeDeclaration).asteriskToken === undefined
                && hasSyntacticModifier(node, ModifierFlags.Async);
    }
    return false;
}

/** @internal */
export function isStringOrNumericLiteralLike(node: Node): node is StringLiteralLike | NumericLiteral {
    return isStringLiteralLike(node) || isNumericLiteral(node);
}

/** @internal */
export function isSignedNumericLiteral(node: Node): node is PrefixUnaryExpression & { operand: NumericLiteral; } {
    return isPrefixUnaryExpression(node) && (node.operator === SyntaxKind.PlusToken || node.operator === SyntaxKind.MinusToken) && isNumericLiteral(node.operand);
}

/**
 * A declaration has a dynamic name if all of the following are true:
 *   1. The declaration has a computed property name.
 *   2. The computed name is *not* expressed as a StringLiteral.
 *   3. The computed name is *not* expressed as a NumericLiteral.
 *   4. The computed name is *not* expressed as a PlusToken or MinusToken
 *      immediately followed by a NumericLiteral.
 *
 * @internal
 */
export function hasDynamicName(declaration: Declaration): declaration is DynamicNamedDeclaration | DynamicNamedBinaryExpression {
    const name = getNameOfDeclaration(declaration);
    return !!name && isDynamicName(name);
}

/** @internal */
export function isDynamicName(name: DeclarationName): boolean {
    if (!(name.kind === SyntaxKind.ComputedPropertyName || name.kind === SyntaxKind.ElementAccessExpression)) {
        return false;
    }
    const expr = isElementAccessExpression(name) ? skipParentheses(name.argumentExpression) : name.expression;
    return !isStringOrNumericLiteralLike(expr) &&
        !isSignedNumericLiteral(expr);
}

/** @internal */
export function getPropertyNameForPropertyNameNode(name: PropertyName | JsxAttributeName): __String | undefined {
    switch (name.kind) {
        case SyntaxKind.Identifier:
        case SyntaxKind.PrivateIdentifier:
            return name.escapedText;
        case SyntaxKind.StringLiteral:
        case SyntaxKind.NoSubstitutionTemplateLiteral:
        case SyntaxKind.NumericLiteral:
            return escapeLeadingUnderscores(name.text);
        case SyntaxKind.ComputedPropertyName:
            const nameExpression = name.expression;
            if (isStringOrNumericLiteralLike(nameExpression)) {
                return escapeLeadingUnderscores(nameExpression.text);
            }
            else if (isSignedNumericLiteral(nameExpression)) {
                if (nameExpression.operator === SyntaxKind.MinusToken) {
                    return tokenToString(nameExpression.operator) + nameExpression.operand.text as __String;
                }
                return nameExpression.operand.text as __String;
            }
            return undefined;
        case SyntaxKind.JsxNamespacedName:
            return getEscapedTextOfJsxNamespacedName(name);
        default:
            return Debug.assertNever(name);
    }
}

/** @internal */
export function isPropertyNameLiteral(node: Node): node is PropertyNameLiteral {
    switch (node.kind) {
        case SyntaxKind.Identifier:
        case SyntaxKind.StringLiteral:
        case SyntaxKind.NoSubstitutionTemplateLiteral:
        case SyntaxKind.NumericLiteral:
            return true;
        default:
            return false;
    }
}
/** @internal */
export function getTextOfIdentifierOrLiteral(node: PropertyNameLiteral | PrivateIdentifier): string {
    return isMemberName(node) ? idText(node) : isJsxNamespacedName(node) ? getTextOfJsxNamespacedName(node) : node.text;
}

/** @internal */
export function getEscapedTextOfIdentifierOrLiteral(node: PropertyNameLiteral): __String {
    return isMemberName(node) ? node.escapedText : isJsxNamespacedName(node) ? getEscapedTextOfJsxNamespacedName(node) : escapeLeadingUnderscores(node.text);
}

/** @internal */
export function getPropertyNameForUniqueESSymbol(symbol: Symbol): __String {
    return `__@${getSymbolId(symbol)}@${symbol.escapedName}` as __String;
}

/** @internal */
export function getSymbolNameForPrivateIdentifier(containingClassSymbol: Symbol, description: __String): __String {
    return `__#${getSymbolId(containingClassSymbol)}@${description}` as __String;
}

/** @internal */
export function isKnownSymbol(symbol: Symbol): boolean {
    return startsWith(symbol.escapedName as string, "__@");
}

/** @internal */
export function isPrivateIdentifierSymbol(symbol: Symbol): boolean {
    return startsWith(symbol.escapedName as string, "__#");
}

/**
 * Includes the word "Symbol" with unicode escapes
 *
 * @internal
 */
export function isESSymbolIdentifier(node: Node): boolean {
    return node.kind === SyntaxKind.Identifier && (node as Identifier).escapedText === "Symbol";
}

/**
 * Indicates whether a property name is the special `__proto__` property.
 * Per the ECMA-262 spec, this only matters for property assignments whose name is
 * the Identifier `__proto__`, or the string literal `"__proto__"`, but not for
 * computed property names.
 *
 * @internal
 */
export function isProtoSetter(node: PropertyName) {
    return isIdentifier(node) ? idText(node) === "__proto__" :
        isStringLiteral(node) && node.text === "__proto__";
}

/** @internal */
export type AnonymousFunctionDefinition =
    | ClassExpression & { readonly name?: undefined; }
    | FunctionExpression & { readonly name?: undefined; }
    | ArrowFunction;

/**
 * Indicates whether an expression is an anonymous function definition.
 *
 * @see https://tc39.es/ecma262/#sec-isanonymousfunctiondefinition
 * @internal
 */
export function isAnonymousFunctionDefinition(node: Expression, cb?: (node: AnonymousFunctionDefinition) => boolean): node is WrappedExpression<AnonymousFunctionDefinition> {
    node = skipOuterExpressions(node);
    switch (node.kind) {
        case SyntaxKind.ClassExpression:
            if (classHasDeclaredOrExplicitlyAssignedName(node as ClassExpression)) {
                return false;
            }
            break;
        case SyntaxKind.FunctionExpression:
            if ((node as FunctionExpression).name) {
                return false;
            }
            break;
        case SyntaxKind.ArrowFunction:
            break;
        default:
            return false;
    }
    return typeof cb === "function" ? cb(node as AnonymousFunctionDefinition) : true;
}

/** @internal */
export type NamedEvaluationSource =
    | PropertyAssignment & { readonly name: Identifier; }
    | ShorthandPropertyAssignment & { readonly objectAssignmentInitializer: Expression; }
    | VariableDeclaration & { readonly name: Identifier; readonly initializer: Expression; }
    | ParameterDeclaration & { readonly name: Identifier; readonly initializer: Expression; readonly dotDotDotToken: undefined; }
    | BindingElement & { readonly name: Identifier; readonly initializer: Expression; readonly dotDotDotToken: undefined; }
    | PropertyDeclaration & { readonly initializer: Expression; }
    | AssignmentExpression<EqualsToken | AmpersandAmpersandEqualsToken | BarBarEqualsToken | QuestionQuestionEqualsToken> & { readonly left: Identifier; }
    | ExportAssignment;

/**
 * Indicates whether a node is a potential source of an assigned name for a class, function, or arrow function.
 *
 * @internal
 */
export function isNamedEvaluationSource(node: Node): node is NamedEvaluationSource {
    switch (node.kind) {
        case SyntaxKind.PropertyAssignment:
            return !isProtoSetter((node as PropertyAssignment).name);
        case SyntaxKind.ShorthandPropertyAssignment:
            return !!(node as ShorthandPropertyAssignment).objectAssignmentInitializer;
        case SyntaxKind.VariableDeclaration:
            return isIdentifier((node as VariableDeclaration).name) && !!(node as VariableDeclaration).initializer;
        case SyntaxKind.Parameter:
            return isIdentifier((node as ParameterDeclaration).name) && !!(node as VariableDeclaration).initializer && !(node as BindingElement).dotDotDotToken;
        case SyntaxKind.BindingElement:
            return isIdentifier((node as BindingElement).name) && !!(node as VariableDeclaration).initializer && !(node as BindingElement).dotDotDotToken;
        case SyntaxKind.PropertyDeclaration:
            return !!(node as PropertyDeclaration).initializer;
        case SyntaxKind.BinaryExpression:
            switch ((node as BinaryExpression).operatorToken.kind) {
                case SyntaxKind.EqualsToken:
                case SyntaxKind.AmpersandAmpersandEqualsToken:
                case SyntaxKind.BarBarEqualsToken:
                case SyntaxKind.QuestionQuestionEqualsToken:
                    return isIdentifier((node as BinaryExpression).left);
            }
            break;
        case SyntaxKind.ExportAssignment:
            return true;
    }
    return false;
}

/** @internal */
export type NamedEvaluation =
    | PropertyAssignment & { readonly name: Identifier; readonly initializer: WrappedExpression<AnonymousFunctionDefinition>; }
    | ShorthandPropertyAssignment & { readonly objectAssignmentInitializer: WrappedExpression<AnonymousFunctionDefinition>; }
    | VariableDeclaration & { readonly name: Identifier; readonly initializer: WrappedExpression<AnonymousFunctionDefinition>; }
    | ParameterDeclaration & { readonly name: Identifier; readonly dotDotDotToken: undefined; readonly initializer: WrappedExpression<AnonymousFunctionDefinition>; }
    | BindingElement & { readonly name: Identifier; readonly dotDotDotToken: undefined; readonly initializer: WrappedExpression<AnonymousFunctionDefinition>; }
    | PropertyDeclaration & { readonly initializer: WrappedExpression<AnonymousFunctionDefinition>; }
    | AssignmentExpression<EqualsToken> & { readonly left: Identifier; readonly right: WrappedExpression<AnonymousFunctionDefinition>; }
    | AssignmentExpression<AmpersandAmpersandEqualsToken | BarBarEqualsToken | QuestionQuestionEqualsToken> & { readonly left: Identifier; readonly right: WrappedExpression<AnonymousFunctionDefinition>; }
    | ExportAssignment & { readonly expression: WrappedExpression<AnonymousFunctionDefinition>; };

/** @internal */
export function isNamedEvaluation(node: Node, cb?: (node: AnonymousFunctionDefinition) => boolean): node is NamedEvaluation {
    if (!isNamedEvaluationSource(node)) return false;
    switch (node.kind) {
        case SyntaxKind.PropertyAssignment:
            return isAnonymousFunctionDefinition(node.initializer, cb);
        case SyntaxKind.ShorthandPropertyAssignment:
            return isAnonymousFunctionDefinition(node.objectAssignmentInitializer, cb);
        case SyntaxKind.VariableDeclaration:
        case SyntaxKind.Parameter:
        case SyntaxKind.BindingElement:
        case SyntaxKind.PropertyDeclaration:
            return isAnonymousFunctionDefinition(node.initializer, cb);
        case SyntaxKind.BinaryExpression:
            return isAnonymousFunctionDefinition(node.right, cb);
        case SyntaxKind.ExportAssignment:
            return isAnonymousFunctionDefinition(node.expression, cb);
    }
}

/** @internal */
export function isPushOrUnshiftIdentifier(node: Identifier) {
    return node.escapedText === "push" || node.escapedText === "unshift";
}

// TODO(jakebailey): this function should not be named this. While it does technically
// return true if the argument is a ParameterDeclaration, it also returns true for nodes
// that are children of ParameterDeclarations inside binding elements.
// Probably, this should be called `rootDeclarationIsParameter`.
/**
 * This function returns true if the this node's root declaration is a parameter.
 * For example, passing a `ParameterDeclaration` will return true, as will passing a
 * binding element that is a child of a `ParameterDeclaration`.
 *
 * If you are looking to test that a `Node` is a `ParameterDeclaration`, use `isParameter`.
 *
 * @internal
 */
export function isParameterDeclaration(node: Declaration): boolean {
    const root = getRootDeclaration(node);
    return root.kind === SyntaxKind.Parameter;
}

/** @internal */
export function getRootDeclaration(node: Node): Node {
    while (node.kind === SyntaxKind.BindingElement) {
        node = node.parent.parent;
    }
    return node;
}

/** @internal */
export function nodeStartsNewLexicalEnvironment(node: Node): boolean {
    const kind = node.kind;
    return kind === SyntaxKind.Constructor
        || kind === SyntaxKind.FunctionExpression
        || kind === SyntaxKind.FunctionDeclaration
        || kind === SyntaxKind.ArrowFunction
        || kind === SyntaxKind.MethodDeclaration
        || kind === SyntaxKind.GetAccessor
        || kind === SyntaxKind.SetAccessor
        || kind === SyntaxKind.ModuleDeclaration
        || kind === SyntaxKind.SourceFile;
}

/** @internal */
export function nodeIsSynthesized(range: TextRange): boolean {
    return positionIsSynthesized(range.pos)
        || positionIsSynthesized(range.end);
}

/** @internal */
export function getOriginalSourceFile(sourceFile: SourceFile) {
    return getParseTreeNode(sourceFile, isSourceFile) || sourceFile;
}

/** @internal */
export const enum Associativity {
    Left,
    Right,
}

/** @internal */
export function getExpressionAssociativity(expression: Expression) {
    const operator = getOperator(expression);
    const hasArguments = expression.kind === SyntaxKind.NewExpression && (expression as NewExpression).arguments !== undefined;
    return getOperatorAssociativity(expression.kind, operator, hasArguments);
}

/** @internal */
export function getOperatorAssociativity(kind: SyntaxKind, operator: SyntaxKind, hasArguments?: boolean) {
    switch (kind) {
        case SyntaxKind.NewExpression:
            return hasArguments ? Associativity.Left : Associativity.Right;

        case SyntaxKind.PrefixUnaryExpression:
        case SyntaxKind.TypeOfExpression:
        case SyntaxKind.VoidExpression:
        case SyntaxKind.DeleteExpression:
        case SyntaxKind.AwaitExpression:
        case SyntaxKind.ConditionalExpression:
        case SyntaxKind.YieldExpression:
            return Associativity.Right;

        case SyntaxKind.BinaryExpression:
            switch (operator) {
                case SyntaxKind.AsteriskAsteriskToken:
                case SyntaxKind.EqualsToken:
                case SyntaxKind.PlusEqualsToken:
                case SyntaxKind.MinusEqualsToken:
                case SyntaxKind.AsteriskAsteriskEqualsToken:
                case SyntaxKind.AsteriskEqualsToken:
                case SyntaxKind.SlashEqualsToken:
                case SyntaxKind.PercentEqualsToken:
                case SyntaxKind.LessThanLessThanEqualsToken:
                case SyntaxKind.GreaterThanGreaterThanEqualsToken:
                case SyntaxKind.GreaterThanGreaterThanGreaterThanEqualsToken:
                case SyntaxKind.AmpersandEqualsToken:
                case SyntaxKind.CaretEqualsToken:
                case SyntaxKind.BarEqualsToken:
                case SyntaxKind.BarBarEqualsToken:
                case SyntaxKind.AmpersandAmpersandEqualsToken:
                case SyntaxKind.QuestionQuestionEqualsToken:
                    return Associativity.Right;
            }
    }
    return Associativity.Left;
}

/** @internal */
export function getExpressionPrecedence(expression: Expression) {
    const operator = getOperator(expression);
    const hasArguments = expression.kind === SyntaxKind.NewExpression && (expression as NewExpression).arguments !== undefined;
    return getOperatorPrecedence(expression.kind, operator, hasArguments);
}

/** @internal */
export function getOperator(expression: Expression): SyntaxKind {
    if (expression.kind === SyntaxKind.BinaryExpression) {
        return (expression as BinaryExpression).operatorToken.kind;
    }
    else if (expression.kind === SyntaxKind.PrefixUnaryExpression || expression.kind === SyntaxKind.PostfixUnaryExpression) {
        return (expression as PrefixUnaryExpression | PostfixUnaryExpression).operator;
    }
    else {
        return expression.kind;
    }
}

/** @internal */
export const enum OperatorPrecedence {
    // Expression:
    //     AssignmentExpression
    //     Expression `,` AssignmentExpression
    Comma,

    // NOTE: `Spread` is higher than `Comma` due to how it is parsed in |ElementList|
    // SpreadElement:
    //     `...` AssignmentExpression
    Spread,

    // AssignmentExpression:
    //     ConditionalExpression
    //     YieldExpression
    //     ArrowFunction
    //     AsyncArrowFunction
    //     LeftHandSideExpression `=` AssignmentExpression
    //     LeftHandSideExpression AssignmentOperator AssignmentExpression
    //
    // NOTE: AssignmentExpression is broken down into several precedences due to the requirements
    //       of the parenthesizer rules.

    // AssignmentExpression: YieldExpression
    // YieldExpression:
    //     `yield`
    //     `yield` AssignmentExpression
    //     `yield` `*` AssignmentExpression
    Yield,

    // AssignmentExpression: LeftHandSideExpression `=` AssignmentExpression
    // AssignmentExpression: LeftHandSideExpression AssignmentOperator AssignmentExpression
    // AssignmentOperator: one of
    //     `*=` `/=` `%=` `+=` `-=` `<<=` `>>=` `>>>=` `&=` `^=` `|=` `**=`
    Assignment,

    // NOTE: `Conditional` is considered higher than `Assignment` here, but in reality they have
    //       the same precedence.
    // AssignmentExpression: ConditionalExpression
    // ConditionalExpression:
    //     ShortCircuitExpression
    //     ShortCircuitExpression `?` AssignmentExpression `:` AssignmentExpression
    // ShortCircuitExpression:
    //     LogicalORExpression
    //     CoalesceExpression
    Conditional,

    // CoalesceExpression:
    //     CoalesceExpressionHead `??` BitwiseORExpression
    // CoalesceExpressionHead:
    //     CoalesceExpression
    //     BitwiseORExpression
    Coalesce = Conditional, // NOTE: This is wrong

    // LogicalORExpression:
    //     LogicalANDExpression
    //     LogicalORExpression `||` LogicalANDExpression
    LogicalOR,

    // LogicalANDExpression:
    //     BitwiseORExpression
    //     LogicalANDExprerssion `&&` BitwiseORExpression
    LogicalAND,

    // BitwiseORExpression:
    //     BitwiseXORExpression
    //     BitwiseORExpression `^` BitwiseXORExpression
    BitwiseOR,

    // BitwiseXORExpression:
    //     BitwiseANDExpression
    //     BitwiseXORExpression `^` BitwiseANDExpression
    BitwiseXOR,

    // BitwiseANDExpression:
    //     EqualityExpression
    //     BitwiseANDExpression `^` EqualityExpression
    BitwiseAND,

    // EqualityExpression:
    //     RelationalExpression
    //     EqualityExpression `==` RelationalExpression
    //     EqualityExpression `!=` RelationalExpression
    //     EqualityExpression `===` RelationalExpression
    //     EqualityExpression `!==` RelationalExpression
    Equality,

    // RelationalExpression:
    //     ShiftExpression
    //     RelationalExpression `<` ShiftExpression
    //     RelationalExpression `>` ShiftExpression
    //     RelationalExpression `<=` ShiftExpression
    //     RelationalExpression `>=` ShiftExpression
    //     RelationalExpression `instanceof` ShiftExpression
    //     RelationalExpression `in` ShiftExpression
    //     [+TypeScript] RelationalExpression `as` Type
    Relational,

    // ShiftExpression:
    //     AdditiveExpression
    //     ShiftExpression `<<` AdditiveExpression
    //     ShiftExpression `>>` AdditiveExpression
    //     ShiftExpression `>>>` AdditiveExpression
    Shift,

    // AdditiveExpression:
    //     MultiplicativeExpression
    //     AdditiveExpression `+` MultiplicativeExpression
    //     AdditiveExpression `-` MultiplicativeExpression
    Additive,

    // MultiplicativeExpression:
    //     ExponentiationExpression
    //     MultiplicativeExpression MultiplicativeOperator ExponentiationExpression
    // MultiplicativeOperator: one of `*`, `/`, `%`
    Multiplicative,

    // ExponentiationExpression:
    //     UnaryExpression
    //     UpdateExpression `**` ExponentiationExpression
    Exponentiation,

    // UnaryExpression:
    //     UpdateExpression
    //     `delete` UnaryExpression
    //     `void` UnaryExpression
    //     `typeof` UnaryExpression
    //     `+` UnaryExpression
    //     `-` UnaryExpression
    //     `~` UnaryExpression
    //     `!` UnaryExpression
    //     AwaitExpression
    // UpdateExpression:            // TODO: Do we need to investigate the precedence here?
    //     `++` UnaryExpression
    //     `--` UnaryExpression
    Unary,

    // UpdateExpression:
    //     LeftHandSideExpression
    //     LeftHandSideExpression `++`
    //     LeftHandSideExpression `--`
    Update,

    // LeftHandSideExpression:
    //     NewExpression
    //     CallExpression
    // NewExpression:
    //     MemberExpression
    //     `new` NewExpression
    LeftHandSide,

    // CallExpression:
    //     CoverCallExpressionAndAsyncArrowHead
    //     SuperCall
    //     ImportCall
    //     CallExpression Arguments
    //     CallExpression `[` Expression `]`
    //     CallExpression `.` IdentifierName
    //     CallExpression TemplateLiteral
    // MemberExpression:
    //     PrimaryExpression
    //     MemberExpression `[` Expression `]`
    //     MemberExpression `.` IdentifierName
    //     MemberExpression TemplateLiteral
    //     SuperProperty
    //     MetaProperty
    //     `new` MemberExpression Arguments
    Member,

    // TODO: JSXElement?
    // PrimaryExpression:
    //     `this`
    //     IdentifierReference
    //     Literal
    //     ArrayLiteral
    //     ObjectLiteral
    //     FunctionExpression
    //     ClassExpression
    //     GeneratorExpression
    //     AsyncFunctionExpression
    //     AsyncGeneratorExpression
    //     RegularExpressionLiteral
    //     TemplateLiteral
    //     CoverParenthesizedExpressionAndArrowParameterList
    Primary,

    Highest = Primary,
    Lowest = Comma,
    // -1 is lower than all other precedences. Returning it will cause binary expression
    // parsing to stop.
    Invalid = -1,
}

/** @internal */
export function getOperatorPrecedence(nodeKind: SyntaxKind, operatorKind: SyntaxKind, hasArguments?: boolean) {
    switch (nodeKind) {
        case SyntaxKind.CommaListExpression:
            return OperatorPrecedence.Comma;

        case SyntaxKind.SpreadElement:
            return OperatorPrecedence.Spread;

        case SyntaxKind.YieldExpression:
            return OperatorPrecedence.Yield;

        case SyntaxKind.ConditionalExpression:
            return OperatorPrecedence.Conditional;

        case SyntaxKind.BinaryExpression:
            switch (operatorKind) {
                case SyntaxKind.CommaToken:
                    return OperatorPrecedence.Comma;

                case SyntaxKind.EqualsToken:
                case SyntaxKind.PlusEqualsToken:
                case SyntaxKind.MinusEqualsToken:
                case SyntaxKind.AsteriskAsteriskEqualsToken:
                case SyntaxKind.AsteriskEqualsToken:
                case SyntaxKind.SlashEqualsToken:
                case SyntaxKind.PercentEqualsToken:
                case SyntaxKind.LessThanLessThanEqualsToken:
                case SyntaxKind.GreaterThanGreaterThanEqualsToken:
                case SyntaxKind.GreaterThanGreaterThanGreaterThanEqualsToken:
                case SyntaxKind.AmpersandEqualsToken:
                case SyntaxKind.CaretEqualsToken:
                case SyntaxKind.BarEqualsToken:
                case SyntaxKind.BarBarEqualsToken:
                case SyntaxKind.AmpersandAmpersandEqualsToken:
                case SyntaxKind.QuestionQuestionEqualsToken:
                    return OperatorPrecedence.Assignment;

                default:
                    return getBinaryOperatorPrecedence(operatorKind);
            }

        // TODO: Should prefix `++` and `--` be moved to the `Update` precedence?
        case SyntaxKind.TypeAssertionExpression:
        case SyntaxKind.NonNullExpression:
        case SyntaxKind.PrefixUnaryExpression:
        case SyntaxKind.TypeOfExpression:
        case SyntaxKind.VoidExpression:
        case SyntaxKind.DeleteExpression:
        case SyntaxKind.AwaitExpression:
            return OperatorPrecedence.Unary;

        case SyntaxKind.PostfixUnaryExpression:
            return OperatorPrecedence.Update;

        case SyntaxKind.CallExpression:
            return OperatorPrecedence.LeftHandSide;

        case SyntaxKind.NewExpression:
            return hasArguments ? OperatorPrecedence.Member : OperatorPrecedence.LeftHandSide;

        case SyntaxKind.TaggedTemplateExpression:
        case SyntaxKind.PropertyAccessExpression:
        case SyntaxKind.ElementAccessExpression:
        case SyntaxKind.MetaProperty:
            return OperatorPrecedence.Member;

        case SyntaxKind.AsExpression:
        case SyntaxKind.SatisfiesExpression:
            return OperatorPrecedence.Relational;

        case SyntaxKind.ThisKeyword:
        case SyntaxKind.SuperKeyword:
        case SyntaxKind.Identifier:
        case SyntaxKind.PrivateIdentifier:
        case SyntaxKind.NullKeyword:
        case SyntaxKind.TrueKeyword:
        case SyntaxKind.FalseKeyword:
        case SyntaxKind.NumericLiteral:
        case SyntaxKind.BigIntLiteral:
        case SyntaxKind.StringLiteral:
        case SyntaxKind.ArrayLiteralExpression:
        case SyntaxKind.ObjectLiteralExpression:
        case SyntaxKind.FunctionExpression:
        case SyntaxKind.ArrowFunction:
        case SyntaxKind.ClassExpression:
        case SyntaxKind.RegularExpressionLiteral:
        case SyntaxKind.NoSubstitutionTemplateLiteral:
        case SyntaxKind.TemplateExpression:
        case SyntaxKind.ParenthesizedExpression:
        case SyntaxKind.OmittedExpression:
        case SyntaxKind.JsxElement:
        case SyntaxKind.JsxSelfClosingElement:
        case SyntaxKind.JsxFragment:
            return OperatorPrecedence.Primary;

        default:
            return OperatorPrecedence.Invalid;
    }
}

/** @internal */
export function getBinaryOperatorPrecedence(kind: SyntaxKind): OperatorPrecedence {
    switch (kind) {
        case SyntaxKind.QuestionQuestionToken:
            return OperatorPrecedence.Coalesce;
        case SyntaxKind.BarBarToken:
            return OperatorPrecedence.LogicalOR;
        case SyntaxKind.AmpersandAmpersandToken:
            return OperatorPrecedence.LogicalAND;
        case SyntaxKind.BarToken:
            return OperatorPrecedence.BitwiseOR;
        case SyntaxKind.CaretToken:
            return OperatorPrecedence.BitwiseXOR;
        case SyntaxKind.AmpersandToken:
            return OperatorPrecedence.BitwiseAND;
        case SyntaxKind.EqualsEqualsToken:
        case SyntaxKind.ExclamationEqualsToken:
        case SyntaxKind.EqualsEqualsEqualsToken:
        case SyntaxKind.ExclamationEqualsEqualsToken:
            return OperatorPrecedence.Equality;
        case SyntaxKind.LessThanToken:
        case SyntaxKind.GreaterThanToken:
        case SyntaxKind.LessThanEqualsToken:
        case SyntaxKind.GreaterThanEqualsToken:
        case SyntaxKind.InstanceOfKeyword:
        case SyntaxKind.InKeyword:
        case SyntaxKind.AsKeyword:
        case SyntaxKind.SatisfiesKeyword:
            return OperatorPrecedence.Relational;
        case SyntaxKind.LessThanLessThanToken:
        case SyntaxKind.GreaterThanGreaterThanToken:
        case SyntaxKind.GreaterThanGreaterThanGreaterThanToken:
            return OperatorPrecedence.Shift;
        case SyntaxKind.PlusToken:
        case SyntaxKind.MinusToken:
            return OperatorPrecedence.Additive;
        case SyntaxKind.AsteriskToken:
        case SyntaxKind.SlashToken:
        case SyntaxKind.PercentToken:
            return OperatorPrecedence.Multiplicative;
        case SyntaxKind.AsteriskAsteriskToken:
            return OperatorPrecedence.Exponentiation;
    }

    // -1 is lower than all other precedences.  Returning it will cause binary expression
    // parsing to stop.
    return -1;
}

/** @internal */
export function getSemanticJsxChildren(children: readonly JsxChild[]) {
    return filter(children, i => {
        switch (i.kind) {
            case SyntaxKind.JsxExpression:
                return !!i.expression;
            case SyntaxKind.JsxText:
                return !i.containsOnlyTriviaWhiteSpaces;
            default:
                return true;
        }
    });
}

/** @internal */
export function createDiagnosticCollection(): DiagnosticCollection {
    let nonFileDiagnostics = [] as Diagnostic[] as SortedArray<Diagnostic>; // See GH#19873
    const filesWithDiagnostics = [] as string[] as SortedArray<string>;
    const fileDiagnostics = new Map<string, SortedArray<DiagnosticWithLocation>>();
    let hasReadNonFileDiagnostics = false;

    return {
        add,
        lookup,
        getGlobalDiagnostics,
        getDiagnostics,
    };

    function lookup(diagnostic: Diagnostic): Diagnostic | undefined {
        let diagnostics: SortedArray<Diagnostic> | undefined;
        if (diagnostic.file) {
            diagnostics = fileDiagnostics.get(diagnostic.file.fileName);
        }
        else {
            diagnostics = nonFileDiagnostics;
        }
        if (!diagnostics) {
            return undefined;
        }
        const result = binarySearch(diagnostics, diagnostic, identity, compareDiagnosticsSkipRelatedInformation);
        if (result >= 0) {
            return diagnostics[result];
        }
        return undefined;
    }

    function add(diagnostic: Diagnostic): void {
        let diagnostics: SortedArray<Diagnostic> | undefined;
        if (diagnostic.file) {
            diagnostics = fileDiagnostics.get(diagnostic.file.fileName);
            if (!diagnostics) {
                diagnostics = [] as Diagnostic[] as SortedArray<DiagnosticWithLocation>; // See GH#19873
                fileDiagnostics.set(diagnostic.file.fileName, diagnostics as SortedArray<DiagnosticWithLocation>);
                insertSorted(filesWithDiagnostics, diagnostic.file.fileName, compareStringsCaseSensitive);
            }
        }
        else {
            // If we've already read the non-file diagnostics, do not modify the existing array.
            if (hasReadNonFileDiagnostics) {
                hasReadNonFileDiagnostics = false;
                nonFileDiagnostics = nonFileDiagnostics.slice() as SortedArray<Diagnostic>;
            }

            diagnostics = nonFileDiagnostics;
        }

        insertSorted(diagnostics, diagnostic, compareDiagnosticsSkipRelatedInformation);
    }

    function getGlobalDiagnostics(): Diagnostic[] {
        hasReadNonFileDiagnostics = true;
        return nonFileDiagnostics;
    }

    function getDiagnostics(fileName: string): DiagnosticWithLocation[];
    function getDiagnostics(): Diagnostic[];
    function getDiagnostics(fileName?: string): Diagnostic[] {
        if (fileName) {
            return fileDiagnostics.get(fileName) || [];
        }

        const fileDiags: Diagnostic[] = flatMapToMutable(filesWithDiagnostics, f => fileDiagnostics.get(f));
        if (!nonFileDiagnostics.length) {
            return fileDiags;
        }
        fileDiags.unshift(...nonFileDiagnostics);
        return fileDiags;
    }
}

const templateSubstitutionRegExp = /\$\{/g;
/** @internal */
export function escapeTemplateSubstitution(str: string): string {
    return str.replace(templateSubstitutionRegExp, "\\${");
}

function containsInvalidEscapeFlag(node: TemplateLiteralToken): boolean {
    return !!((node.templateFlags || 0) & TokenFlags.ContainsInvalidEscape);
}

/** @internal */
export function hasInvalidEscape(template: TemplateLiteral): boolean {
    return template && !!(isNoSubstitutionTemplateLiteral(template)
        ? containsInvalidEscapeFlag(template)
        : (containsInvalidEscapeFlag(template.head) || some(template.templateSpans, span => containsInvalidEscapeFlag(span.literal))));
}

// This consists of the first 19 unprintable ASCII characters, canonical escapes, lineSeparator,
// paragraphSeparator, and nextLine. The latter three are just desirable to suppress new lines in
// the language service. These characters should be escaped when printing, and if any characters are added,
// the map below must be updated. Note that this regexp *does not* include the 'delete' character.
// There is no reason for this other than that JSON.stringify does not handle it either.
const doubleQuoteEscapedCharsRegExp = /[\\"\u0000-\u001f\t\v\f\b\r\n\u2028\u2029\u0085]/g;
const singleQuoteEscapedCharsRegExp = /[\\'\u0000-\u001f\t\v\f\b\r\n\u2028\u2029\u0085]/g;
// Template strings preserve simple LF newlines, still encode CRLF (or CR)
const backtickQuoteEscapedCharsRegExp = /\r\n|[\\`\u0000-\u001f\t\v\f\b\r\u2028\u2029\u0085]/g;
const escapedCharsMap = new Map(Object.entries({
    "\t": "\\t",
    "\v": "\\v",
    "\f": "\\f",
    "\b": "\\b",
    "\r": "\\r",
    "\n": "\\n",
    "\\": "\\\\",
    '"': '\\"',
    "'": "\\'",
    "`": "\\`",
    "\u2028": "\\u2028", // lineSeparator
    "\u2029": "\\u2029", // paragraphSeparator
    "\u0085": "\\u0085", // nextLine
    "\r\n": "\\r\\n", // special case for CRLFs in backticks
}));

function encodeUtf16EscapeSequence(charCode: number): string {
    const hexCharCode = charCode.toString(16).toUpperCase();
    const paddedHexCode = ("0000" + hexCharCode).slice(-4);
    return "\\u" + paddedHexCode;
}

function getReplacement(c: string, offset: number, input: string) {
    if (c.charCodeAt(0) === CharacterCodes.nullCharacter) {
        const lookAhead = input.charCodeAt(offset + c.length);
        if (lookAhead >= CharacterCodes._0 && lookAhead <= CharacterCodes._9) {
            // If the null character is followed by digits, print as a hex escape to prevent the result from parsing as an octal (which is forbidden in strict mode)
            return "\\x00";
        }
        // Otherwise, keep printing a literal \0 for the null character
        return "\\0";
    }
    return escapedCharsMap.get(c) || encodeUtf16EscapeSequence(c.charCodeAt(0));
}

/**
 * Based heavily on the abstract 'Quote'/'QuoteJSONString' operation from ECMA-262 (24.3.2.2),
 * but augmented for a few select characters (e.g. lineSeparator, paragraphSeparator, nextLine)
 * Note that this doesn't actually wrap the input in double quotes.
 *
 * @internal
 */
export function escapeString(s: string, quoteChar?: CharacterCodes.doubleQuote | CharacterCodes.singleQuote | CharacterCodes.backtick): string {
    const escapedCharsRegExp = quoteChar === CharacterCodes.backtick ? backtickQuoteEscapedCharsRegExp :
        quoteChar === CharacterCodes.singleQuote ? singleQuoteEscapedCharsRegExp :
        doubleQuoteEscapedCharsRegExp;
    return s.replace(escapedCharsRegExp, getReplacement);
}

const nonAsciiCharacters = /[^\u0000-\u007F]/g;
/** @internal */
export function escapeNonAsciiString(s: string, quoteChar?: CharacterCodes.doubleQuote | CharacterCodes.singleQuote | CharacterCodes.backtick): string {
    s = escapeString(s, quoteChar);
    // Replace non-ASCII characters with '\uNNNN' escapes if any exist.
    // Otherwise just return the original string.
    return nonAsciiCharacters.test(s) ?
        s.replace(nonAsciiCharacters, c => encodeUtf16EscapeSequence(c.charCodeAt(0))) :
        s;
}

// This consists of the first 19 unprintable ASCII characters, JSX canonical escapes, lineSeparator,
// paragraphSeparator, and nextLine. The latter three are just desirable to suppress new lines in
// the language service. These characters should be escaped when printing, and if any characters are added,
// the map below must be updated.
const jsxDoubleQuoteEscapedCharsRegExp = /["\u0000-\u001f\u2028\u2029\u0085]/g;
const jsxSingleQuoteEscapedCharsRegExp = /['\u0000-\u001f\u2028\u2029\u0085]/g;
const jsxEscapedCharsMap = new Map(Object.entries({
    '"': "&quot;",
    "'": "&apos;",
}));

function encodeJsxCharacterEntity(charCode: number): string {
    const hexCharCode = charCode.toString(16).toUpperCase();
    return "&#x" + hexCharCode + ";";
}

function getJsxAttributeStringReplacement(c: string) {
    if (c.charCodeAt(0) === CharacterCodes.nullCharacter) {
        return "&#0;";
    }
    return jsxEscapedCharsMap.get(c) || encodeJsxCharacterEntity(c.charCodeAt(0));
}

/** @internal */
export function escapeJsxAttributeString(s: string, quoteChar?: CharacterCodes.doubleQuote | CharacterCodes.singleQuote) {
    const escapedCharsRegExp = quoteChar === CharacterCodes.singleQuote ? jsxSingleQuoteEscapedCharsRegExp :
        jsxDoubleQuoteEscapedCharsRegExp;
    return s.replace(escapedCharsRegExp, getJsxAttributeStringReplacement);
}

/**
 * Strip off existed surrounding single quotes, double quotes, or backticks from a given string
 *
 * @return non-quoted string
 *
 * @internal
 */
export function stripQuotes(name: string) {
    const length = name.length;
    if (length >= 2 && name.charCodeAt(0) === name.charCodeAt(length - 1) && isQuoteOrBacktick(name.charCodeAt(0))) {
        return name.substring(1, length - 1);
    }
    return name;
}

function isQuoteOrBacktick(charCode: number) {
    return charCode === CharacterCodes.singleQuote ||
        charCode === CharacterCodes.doubleQuote ||
        charCode === CharacterCodes.backtick;
}

/** @internal */
export function isIntrinsicJsxName(name: __String | string) {
    const ch = (name as string).charCodeAt(0);
    return (ch >= CharacterCodes.a && ch <= CharacterCodes.z) || (name as string).includes("-");
}

const indentStrings: string[] = ["", "    "];
/** @internal */
export function getIndentString(level: number) {
    // prepopulate cache
    const singleLevel = indentStrings[1];
    for (let current = indentStrings.length; current <= level; current++) {
        indentStrings.push(indentStrings[current - 1] + singleLevel);
    }
    return indentStrings[level];
}

/** @internal */
export function getIndentSize() {
    return indentStrings[1].length;
}

/** @internal */
export function createTextWriter(newLine: string): EmitTextWriter {
    // Why var? It avoids TDZ checks in the runtime which can be costly.
    // See: https://github.com/microsoft/TypeScript/issues/52924
    /* eslint-disable no-var */
    var output: string;
    var indent: number;
    var lineStart: boolean;
    var lineCount: number;
    var linePos: number;
    var hasTrailingComment = false;
    /* eslint-enable no-var */

    function updateLineCountAndPosFor(s: string) {
        const lineStartsOfS = computeLineStarts(s);
        if (lineStartsOfS.length > 1) {
            lineCount = lineCount + lineStartsOfS.length - 1;
            linePos = output.length - s.length + last(lineStartsOfS);
            lineStart = (linePos - output.length) === 0;
        }
        else {
            lineStart = false;
        }
    }

    function writeText(s: string) {
        if (s && s.length) {
            if (lineStart) {
                s = getIndentString(indent) + s;
                lineStart = false;
            }
            output += s;
            updateLineCountAndPosFor(s);
        }
    }

    function write(s: string) {
        if (s) hasTrailingComment = false;
        writeText(s);
    }

    function writeComment(s: string) {
        if (s) hasTrailingComment = true;
        writeText(s);
    }

    function reset(): void {
        output = "";
        indent = 0;
        lineStart = true;
        lineCount = 0;
        linePos = 0;
        hasTrailingComment = false;
    }

    function rawWrite(s: string) {
        if (s !== undefined) {
            output += s;
            updateLineCountAndPosFor(s);
            hasTrailingComment = false;
        }
    }

    function writeLiteral(s: string) {
        if (s && s.length) {
            write(s);
        }
    }

    function writeLine(force?: boolean) {
        if (!lineStart || force) {
            output += newLine;
            lineCount++;
            linePos = output.length;
            lineStart = true;
            hasTrailingComment = false;
        }
    }

    function getTextPosWithWriteLine() {
        return lineStart ? output.length : (output.length + newLine.length);
    }

    reset();

    return {
        write,
        rawWrite,
        writeLiteral,
        writeLine,
        increaseIndent: () => {
            indent++;
        },
        decreaseIndent: () => {
            indent--;
        },
        getIndent: () => indent,
        getTextPos: () => output.length,
        getLine: () => lineCount,
        getColumn: () => lineStart ? indent * getIndentSize() : output.length - linePos,
        getText: () => output,
        isAtStartOfLine: () => lineStart,
        hasTrailingComment: () => hasTrailingComment,
        hasTrailingWhitespace: () => !!output.length && isWhiteSpaceLike(output.charCodeAt(output.length - 1)),
        clear: reset,
        writeKeyword: write,
        writeOperator: write,
        writeParameter: write,
        writeProperty: write,
        writePunctuation: write,
        writeSpace: write,
        writeStringLiteral: write,
        writeSymbol: (s, _) => write(s),
        writeTrailingSemicolon: write,
        writeComment,
        getTextPosWithWriteLine,
    };
}

/** @internal */
export function getTrailingSemicolonDeferringWriter(writer: EmitTextWriter): EmitTextWriter {
    let pendingTrailingSemicolon = false;

    function commitPendingTrailingSemicolon() {
        if (pendingTrailingSemicolon) {
            writer.writeTrailingSemicolon(";");
            pendingTrailingSemicolon = false;
        }
    }

    return {
        ...writer,
        writeTrailingSemicolon() {
            pendingTrailingSemicolon = true;
        },
        writeLiteral(s) {
            commitPendingTrailingSemicolon();
            writer.writeLiteral(s);
        },
        writeStringLiteral(s) {
            commitPendingTrailingSemicolon();
            writer.writeStringLiteral(s);
        },
        writeSymbol(s, sym) {
            commitPendingTrailingSemicolon();
            writer.writeSymbol(s, sym);
        },
        writePunctuation(s) {
            commitPendingTrailingSemicolon();
            writer.writePunctuation(s);
        },
        writeKeyword(s) {
            commitPendingTrailingSemicolon();
            writer.writeKeyword(s);
        },
        writeOperator(s) {
            commitPendingTrailingSemicolon();
            writer.writeOperator(s);
        },
        writeParameter(s) {
            commitPendingTrailingSemicolon();
            writer.writeParameter(s);
        },
        writeSpace(s) {
            commitPendingTrailingSemicolon();
            writer.writeSpace(s);
        },
        writeProperty(s) {
            commitPendingTrailingSemicolon();
            writer.writeProperty(s);
        },
        writeComment(s) {
            commitPendingTrailingSemicolon();
            writer.writeComment(s);
        },
        writeLine() {
            commitPendingTrailingSemicolon();
            writer.writeLine();
        },
        increaseIndent() {
            commitPendingTrailingSemicolon();
            writer.increaseIndent();
        },
        decreaseIndent() {
            commitPendingTrailingSemicolon();
            writer.decreaseIndent();
        },
    };
}

/** @internal */
export function hostUsesCaseSensitiveFileNames(host: { useCaseSensitiveFileNames?(): boolean; }): boolean {
    return host.useCaseSensitiveFileNames ? host.useCaseSensitiveFileNames() : false;
}

/** @internal */
export function hostGetCanonicalFileName(host: { useCaseSensitiveFileNames?(): boolean; }): GetCanonicalFileName {
    return createGetCanonicalFileName(hostUsesCaseSensitiveFileNames(host));
}

/** @internal */
export interface ResolveModuleNameResolutionHost {
    getCanonicalFileName(p: string): string;
    getCommonSourceDirectory(): string;
    getCurrentDirectory(): string;
}

/** @internal */
export function getResolvedExternalModuleName(host: ResolveModuleNameResolutionHost, file: SourceFile, referenceFile?: SourceFile): string {
    return file.moduleName || getExternalModuleNameFromPath(host, file.fileName, referenceFile && referenceFile.fileName);
}

function getCanonicalAbsolutePath(host: ResolveModuleNameResolutionHost, path: string) {
    return host.getCanonicalFileName(getNormalizedAbsolutePath(path, host.getCurrentDirectory()));
}

/** @internal */
export function getExternalModuleNameFromDeclaration(host: ResolveModuleNameResolutionHost, resolver: EmitResolver, declaration: ImportEqualsDeclaration | ImportDeclaration | ExportDeclaration | ModuleDeclaration | ImportTypeNode): string | undefined {
    const file = resolver.getExternalModuleFileFromDeclaration(declaration);
    if (!file || file.isDeclarationFile) {
        return undefined;
    }
    // If the declaration already uses a non-relative name, and is outside the common source directory, continue to use it
    const specifier = getExternalModuleName(declaration);
    if (
        specifier && isStringLiteralLike(specifier) && !pathIsRelative(specifier.text) &&
        !getCanonicalAbsolutePath(host, file.path).includes(getCanonicalAbsolutePath(host, ensureTrailingDirectorySeparator(host.getCommonSourceDirectory())))
    ) {
        return undefined;
    }
    return getResolvedExternalModuleName(host, file);
}

/**
 * Resolves a local path to a path which is absolute to the base of the emit
 *
 * @internal
 */
export function getExternalModuleNameFromPath(host: ResolveModuleNameResolutionHost, fileName: string, referencePath?: string): string {
    const getCanonicalFileName = (f: string) => host.getCanonicalFileName(f);
    const dir = toPath(referencePath ? getDirectoryPath(referencePath) : host.getCommonSourceDirectory(), host.getCurrentDirectory(), getCanonicalFileName);
    const filePath = getNormalizedAbsolutePath(fileName, host.getCurrentDirectory());
    const relativePath = getRelativePathToDirectoryOrUrl(dir, filePath, dir, getCanonicalFileName, /*isAbsolutePathAnUrl*/ false);
    const extensionless = removeFileExtension(relativePath);
    return referencePath ? ensurePathIsNonModuleName(extensionless) : extensionless;
}

/** @internal */
export function getOwnEmitOutputFilePath(fileName: string, host: EmitHost, extension: string) {
    const compilerOptions = host.getCompilerOptions();
    let emitOutputFilePathWithoutExtension: string;
    if (compilerOptions.outDir) {
        emitOutputFilePathWithoutExtension = removeFileExtension(getSourceFilePathInNewDir(fileName, host, compilerOptions.outDir));
    }
    else {
        emitOutputFilePathWithoutExtension = removeFileExtension(fileName);
    }

    return emitOutputFilePathWithoutExtension + extension;
}

/** @internal */
export function getDeclarationEmitOutputFilePath(fileName: string, host: EmitHost) {
    return getDeclarationEmitOutputFilePathWorker(fileName, host.getCompilerOptions(), host.getCurrentDirectory(), host.getCommonSourceDirectory(), f => host.getCanonicalFileName(f));
}

/** @internal */
export function getDeclarationEmitOutputFilePathWorker(fileName: string, options: CompilerOptions, currentDirectory: string, commonSourceDirectory: string, getCanonicalFileName: GetCanonicalFileName): string {
    const outputDir = options.declarationDir || options.outDir; // Prefer declaration folder if specified

    const path = outputDir
        ? getSourceFilePathInNewDirWorker(fileName, outputDir, currentDirectory, commonSourceDirectory, getCanonicalFileName)
        : fileName;
    const declarationExtension = getDeclarationEmitExtensionForPath(path);
    return removeFileExtension(path) + declarationExtension;
}

/** @internal */
export function getDeclarationEmitExtensionForPath(path: string) {
    return fileExtensionIsOneOf(path, [Extension.Mjs, Extension.Mts]) ? Extension.Dmts :
        fileExtensionIsOneOf(path, [Extension.Cjs, Extension.Cts]) ? Extension.Dcts :
        fileExtensionIsOneOf(path, [Extension.Json]) ? `.d.json.ts` : // Drive-by redefinition of json declaration file output name so if it's ever enabled, it behaves well
        Extension.Dts;
}

/**
 * This function is an inverse of `getDeclarationEmitExtensionForPath`.
 *
 * @internal
 */
export function getPossibleOriginalInputExtensionForExtension(path: string) {
    return fileExtensionIsOneOf(path, [Extension.Dmts, Extension.Mjs, Extension.Mts]) ? [Extension.Mts, Extension.Mjs] :
        fileExtensionIsOneOf(path, [Extension.Dcts, Extension.Cjs, Extension.Cts]) ? [Extension.Cts, Extension.Cjs] :
        fileExtensionIsOneOf(path, [`.d.json.ts`]) ? [Extension.Json] :
        [Extension.Tsx, Extension.Ts, Extension.Jsx, Extension.Js];
}

/** @internal */
export function outFile(options: CompilerOptions) {
    return options.outFile || options.out;
}

/**
 * Returns 'undefined' if and only if 'options.paths' is undefined.
 *
 * @internal
 */
export function getPathsBasePath(options: CompilerOptions, host: { getCurrentDirectory?(): string; }) {
    if (!options.paths) return undefined;
    return options.baseUrl ?? Debug.checkDefined(options.pathsBasePath || host.getCurrentDirectory?.(), "Encountered 'paths' without a 'baseUrl', config file, or host 'getCurrentDirectory'.");
}

/** @internal */
export interface EmitFileNames {
    jsFilePath?: string | undefined;
    sourceMapFilePath?: string | undefined;
    declarationFilePath?: string | undefined;
    declarationMapPath?: string | undefined;
    buildInfoPath?: string | undefined;
}

/**
 * Gets the source files that are expected to have an emit output.
 *
 * Originally part of `forEachExpectedEmitFile`, this functionality was extracted to support
 * transformations.
 *
 * @param host An EmitHost.
 * @param targetSourceFile An optional target source file to emit.
 *
 * @internal
 */
export function getSourceFilesToEmit(host: EmitHost, targetSourceFile?: SourceFile, forceDtsEmit?: boolean): readonly SourceFile[] {
    const options = host.getCompilerOptions();
    if (outFile(options)) {
        const moduleKind = getEmitModuleKind(options);
        const moduleEmitEnabled = options.emitDeclarationOnly || moduleKind === ModuleKind.AMD || moduleKind === ModuleKind.System;
        // Can emit only sources that are not declaration file and are either non module code or module with --module or --target es6 specified
        return filter(
            host.getSourceFiles(),
            sourceFile =>
                (moduleEmitEnabled || !isExternalModule(sourceFile)) &&
                sourceFileMayBeEmitted(sourceFile, host, forceDtsEmit),
        );
    }
    else {
        const sourceFiles = targetSourceFile === undefined ? host.getSourceFiles() : [targetSourceFile];
        return filter(
            sourceFiles,
            sourceFile => sourceFileMayBeEmitted(sourceFile, host, forceDtsEmit),
        );
    }
}

/**
 * Don't call this for `--outFile`, just for `--outDir` or plain emit. `--outFile` needs additional checks.
 *
 * @internal
 */
export function sourceFileMayBeEmitted(sourceFile: SourceFile, host: SourceFileMayBeEmittedHost, forceDtsEmit?: boolean) {
    const options = host.getCompilerOptions();
    // Js files are emitted only if option is enabled
    if (options.noEmitForJsFiles && isSourceFileJS(sourceFile)) return false;
    // Declaration files are not emitted
    if (sourceFile.isDeclarationFile) return false;
    // Source file from node_modules are not emitted
    if (host.isSourceFileFromExternalLibrary(sourceFile)) return false;
    // forcing dts emit => file needs to be emitted
    if (forceDtsEmit) return true;
    // Check other conditions for file emit
    // Source files from referenced projects are not emitted
    if (host.isSourceOfProjectReferenceRedirect(sourceFile.fileName)) return false;
    // Any non json file should be emitted
    if (!isJsonSourceFile(sourceFile)) return true;
    if (host.getResolvedProjectReferenceToRedirect(sourceFile.fileName)) return false;
    // Emit json file if outFile is specified
    if (outFile(options)) return true;
    // Json file is not emitted if outDir is not specified
    if (!options.outDir) return false;
    // Otherwise if rootDir or composite config file, we know common sourceDir and can check if file would be emitted in same location
    if (options.rootDir || (options.composite && options.configFilePath)) {
        const commonDir = getNormalizedAbsolutePath(getCommonSourceDirectory(options, () => [], host.getCurrentDirectory(), host.getCanonicalFileName), host.getCurrentDirectory());
        const outputPath = getSourceFilePathInNewDirWorker(sourceFile.fileName, options.outDir, host.getCurrentDirectory(), commonDir, host.getCanonicalFileName);
        if (comparePaths(sourceFile.fileName, outputPath, host.getCurrentDirectory(), !host.useCaseSensitiveFileNames()) === Comparison.EqualTo) return false;
    }
    return true;
}

/** @internal */
export function getSourceFilePathInNewDir(fileName: string, host: EmitHost, newDirPath: string): string {
    return getSourceFilePathInNewDirWorker(fileName, newDirPath, host.getCurrentDirectory(), host.getCommonSourceDirectory(), f => host.getCanonicalFileName(f));
}

/** @internal */
export function getSourceFilePathInNewDirWorker(fileName: string, newDirPath: string, currentDirectory: string, commonSourceDirectory: string, getCanonicalFileName: GetCanonicalFileName): string {
    let sourceFilePath = getNormalizedAbsolutePath(fileName, currentDirectory);
    const isSourceFileInCommonSourceDirectory = getCanonicalFileName(sourceFilePath).indexOf(getCanonicalFileName(commonSourceDirectory)) === 0;
    sourceFilePath = isSourceFileInCommonSourceDirectory ? sourceFilePath.substring(commonSourceDirectory.length) : sourceFilePath;
    return combinePaths(newDirPath, sourceFilePath);
}

/** @internal */
export function writeFile(host: { writeFile: WriteFileCallback; }, diagnostics: DiagnosticCollection, fileName: string, text: string, writeByteOrderMark: boolean, sourceFiles?: readonly SourceFile[], data?: WriteFileCallbackData) {
    host.writeFile(
        fileName,
        text,
        writeByteOrderMark,
        hostErrorMessage => {
            diagnostics.add(createCompilerDiagnostic(Diagnostics.Could_not_write_file_0_Colon_1, fileName, hostErrorMessage));
        },
        sourceFiles,
        data,
    );
}

function ensureDirectoriesExist(
    directoryPath: string,
    createDirectory: (path: string) => void,
    directoryExists: (path: string) => boolean,
): void {
    if (directoryPath.length > getRootLength(directoryPath) && !directoryExists(directoryPath)) {
        const parentDirectory = getDirectoryPath(directoryPath);
        ensureDirectoriesExist(parentDirectory, createDirectory, directoryExists);
        createDirectory(directoryPath);
    }
}

/** @internal */
export function writeFileEnsuringDirectories(
    path: string,
    data: string,
    writeByteOrderMark: boolean,
    writeFile: (path: string, data: string, writeByteOrderMark: boolean) => void,
    createDirectory: (path: string) => void,
    directoryExists: (path: string) => boolean,
): void {
    // PERF: Checking for directory existence is expensive.  Instead, assume the directory exists
    // and fall back to creating it if the file write fails.
    try {
        writeFile(path, data, writeByteOrderMark);
    }
    catch {
        ensureDirectoriesExist(getDirectoryPath(normalizePath(path)), createDirectory, directoryExists);
        writeFile(path, data, writeByteOrderMark);
    }
}

/** @internal */
export function getLineOfLocalPosition(sourceFile: SourceFile, pos: number) {
    const lineStarts = getLineStarts(sourceFile);
    return computeLineOfPosition(lineStarts, pos);
}

/** @internal */
export function getLineOfLocalPositionFromLineMap(lineMap: readonly number[], pos: number) {
    return computeLineOfPosition(lineMap, pos);
}

/** @internal */
export function getFirstConstructorWithBody(node: ClassLikeDeclaration): ConstructorDeclaration & { body: FunctionBody; } | undefined {
    return find(node.members, (member): member is ConstructorDeclaration & { body: FunctionBody; } => isConstructorDeclaration(member) && nodeIsPresent(member.body));
}

/** @internal */
export function getSetAccessorValueParameter(accessor: SetAccessorDeclaration): ParameterDeclaration | undefined {
    if (accessor && accessor.parameters.length > 0) {
        const hasThis = accessor.parameters.length === 2 && parameterIsThisKeyword(accessor.parameters[0]);
        return accessor.parameters[hasThis ? 1 : 0];
    }
}

/**
 * Get the type annotation for the value parameter.
 *
 * @internal
 */
export function getSetAccessorTypeAnnotationNode(accessor: SetAccessorDeclaration): TypeNode | undefined {
    const parameter = getSetAccessorValueParameter(accessor);
    return parameter && parameter.type;
}

/** @internal */
export function getThisParameter(signature: SignatureDeclaration | JSDocSignature): ParameterDeclaration | undefined {
    // callback tags do not currently support this parameters
    if (signature.parameters.length && !isJSDocSignature(signature)) {
        const thisParameter = signature.parameters[0];
        if (parameterIsThisKeyword(thisParameter)) {
            return thisParameter;
        }
    }
}

/** @internal */
export function parameterIsThisKeyword(parameter: ParameterDeclaration): boolean {
    return isThisIdentifier(parameter.name);
}

/** @internal */
export function isThisIdentifier(node: Node | undefined): boolean {
    return !!node && node.kind === SyntaxKind.Identifier && identifierIsThisKeyword(node as Identifier);
}

/** @internal */
export function isInTypeQuery(node: Node): boolean {
    // TypeScript 1.0 spec (April 2014): 3.6.3
    // A type query consists of the keyword typeof followed by an expression.
    // The expression is restricted to a single identifier or a sequence of identifiers separated by periods
    return !!findAncestor(
        node,
        n => n.kind === SyntaxKind.TypeQuery ? true : n.kind === SyntaxKind.Identifier || n.kind === SyntaxKind.QualifiedName ? false : "quit",
    );
}

/** @internal */
export function isThisInTypeQuery(node: Node): boolean {
    if (!isThisIdentifier(node)) {
        return false;
    }

    while (isQualifiedName(node.parent) && node.parent.left === node) {
        node = node.parent;
    }

    return node.parent.kind === SyntaxKind.TypeQuery;
}

/** @internal */
export function identifierIsThisKeyword(id: Identifier): boolean {
    return id.escapedText === "this";
}

/** @internal */
export function getAllAccessorDeclarations(declarations: readonly Declaration[], accessor: AccessorDeclaration): AllAccessorDeclarations {
    // TODO: GH#18217
    let firstAccessor!: AccessorDeclaration;
    let secondAccessor!: AccessorDeclaration;
    let getAccessor!: GetAccessorDeclaration;
    let setAccessor!: SetAccessorDeclaration;
    if (hasDynamicName(accessor)) {
        firstAccessor = accessor;
        if (accessor.kind === SyntaxKind.GetAccessor) {
            getAccessor = accessor;
        }
        else if (accessor.kind === SyntaxKind.SetAccessor) {
            setAccessor = accessor;
        }
        else {
            Debug.fail("Accessor has wrong kind");
        }
    }
    else {
        forEach(declarations, member => {
            if (
                isAccessor(member)
                && isStatic(member) === isStatic(accessor)
            ) {
                const memberName = getPropertyNameForPropertyNameNode(member.name);
                const accessorName = getPropertyNameForPropertyNameNode(accessor.name);
                if (memberName === accessorName) {
                    if (!firstAccessor) {
                        firstAccessor = member;
                    }
                    else if (!secondAccessor) {
                        secondAccessor = member;
                    }

                    if (member.kind === SyntaxKind.GetAccessor && !getAccessor) {
                        getAccessor = member;
                    }

                    if (member.kind === SyntaxKind.SetAccessor && !setAccessor) {
                        setAccessor = member;
                    }
                }
            }
        });
    }
    return {
        firstAccessor,
        secondAccessor,
        getAccessor,
        setAccessor,
    };
}

/**
 * Gets the effective type annotation of a variable, parameter, or property. If the node was
 * parsed in a JavaScript file, gets the type annotation from JSDoc.  Also gets the type of
 * functions only the JSDoc case.
 *
 * @internal
 */
export function getEffectiveTypeAnnotationNode(node: Node): TypeNode | undefined {
    if (!isInJSFile(node) && isFunctionDeclaration(node)) return undefined;
    const type = (node as HasType).type;
    if (type || !isInJSFile(node)) return type;
    return isJSDocPropertyLikeTag(node) ? node.typeExpression && node.typeExpression.type : getJSDocType(node);
}

/** @internal */
export function getTypeAnnotationNode(node: Node): TypeNode | undefined {
    return (node as HasType).type;
}

/**
 * Gets the effective return type annotation of a signature. If the node was parsed in a
 * JavaScript file, gets the return type annotation from JSDoc.
 *
 * @internal
 */
export function getEffectiveReturnTypeNode(node: SignatureDeclaration | JSDocSignature): TypeNode | undefined {
    return isJSDocSignature(node) ?
        node.type && node.type.typeExpression && node.type.typeExpression.type :
        node.type || (isInJSFile(node) ? getJSDocReturnType(node) : undefined);
}

/** @internal */
export function getJSDocTypeParameterDeclarations(node: DeclarationWithTypeParameters): readonly TypeParameterDeclaration[] {
    return flatMap(getJSDocTags(node), tag => isNonTypeAliasTemplate(tag) ? tag.typeParameters : undefined);
}

/** template tags are only available when a typedef isn't already using them */
function isNonTypeAliasTemplate(tag: JSDocTag): tag is JSDocTemplateTag {
    return isJSDocTemplateTag(tag) && !(tag.parent.kind === SyntaxKind.JSDoc && (tag.parent.tags!.some(isJSDocTypeAlias) || tag.parent.tags!.some(isJSDocOverloadTag)));
}

/**
 * Gets the effective type annotation of the value parameter of a set accessor. If the node
 * was parsed in a JavaScript file, gets the type annotation from JSDoc.
 *
 * @internal
 */
export function getEffectiveSetAccessorTypeAnnotationNode(node: SetAccessorDeclaration): TypeNode | undefined {
    const parameter = getSetAccessorValueParameter(node);
    return parameter && getEffectiveTypeAnnotationNode(parameter);
}

/** @internal */
export function emitNewLineBeforeLeadingComments(lineMap: readonly number[], writer: EmitTextWriter, node: TextRange, leadingComments: readonly CommentRange[] | undefined) {
    emitNewLineBeforeLeadingCommentsOfPosition(lineMap, writer, node.pos, leadingComments);
}

/** @internal */
export function emitNewLineBeforeLeadingCommentsOfPosition(lineMap: readonly number[], writer: EmitTextWriter, pos: number, leadingComments: readonly CommentRange[] | undefined) {
    // If the leading comments start on different line than the start of node, write new line
    if (
        leadingComments && leadingComments.length && pos !== leadingComments[0].pos &&
        getLineOfLocalPositionFromLineMap(lineMap, pos) !== getLineOfLocalPositionFromLineMap(lineMap, leadingComments[0].pos)
    ) {
        writer.writeLine();
    }
}

/** @internal */
export function emitNewLineBeforeLeadingCommentOfPosition(lineMap: readonly number[], writer: EmitTextWriter, pos: number, commentPos: number) {
    // If the leading comments start on different line than the start of node, write new line
    if (
        pos !== commentPos &&
        getLineOfLocalPositionFromLineMap(lineMap, pos) !== getLineOfLocalPositionFromLineMap(lineMap, commentPos)
    ) {
        writer.writeLine();
    }
}

/** @internal */
export function emitComments(
    text: string,
    lineMap: readonly number[],
    writer: EmitTextWriter,
    comments: readonly CommentRange[] | undefined,
    leadingSeparator: boolean,
    trailingSeparator: boolean,
    newLine: string,
    writeComment: (text: string, lineMap: readonly number[], writer: EmitTextWriter, commentPos: number, commentEnd: number, newLine: string) => void,
) {
    if (comments && comments.length > 0) {
        if (leadingSeparator) {
            writer.writeSpace(" ");
        }

        let emitInterveningSeparator = false;
        for (const comment of comments) {
            if (emitInterveningSeparator) {
                writer.writeSpace(" ");
                emitInterveningSeparator = false;
            }

            writeComment(text, lineMap, writer, comment.pos, comment.end, newLine);
            if (comment.hasTrailingNewLine) {
                writer.writeLine();
            }
            else {
                emitInterveningSeparator = true;
            }
        }

        if (emitInterveningSeparator && trailingSeparator) {
            writer.writeSpace(" ");
        }
    }
}

/**
 * Detached comment is a comment at the top of file or function body that is separated from
 * the next statement by space.
 *
 * @internal
 */
export function emitDetachedComments(text: string, lineMap: readonly number[], writer: EmitTextWriter, writeComment: (text: string, lineMap: readonly number[], writer: EmitTextWriter, commentPos: number, commentEnd: number, newLine: string) => void, node: TextRange, newLine: string, removeComments: boolean) {
    let leadingComments: CommentRange[] | undefined;
    let currentDetachedCommentInfo: { nodePos: number; detachedCommentEndPos: number; } | undefined;
    if (removeComments) {
        // removeComments is true, only reserve pinned comment at the top of file
        // For example:
        //      /*! Pinned Comment */
        //
        //      var x = 10;
        if (node.pos === 0) {
            leadingComments = filter(getLeadingCommentRanges(text, node.pos), isPinnedCommentLocal);
        }
    }
    else {
        // removeComments is false, just get detached as normal and bypass the process to filter comment
        leadingComments = getLeadingCommentRanges(text, node.pos);
    }

    if (leadingComments) {
        const detachedComments: CommentRange[] = [];
        let lastComment: CommentRange | undefined;

        for (const comment of leadingComments) {
            if (lastComment) {
                const lastCommentLine = getLineOfLocalPositionFromLineMap(lineMap, lastComment.end);
                const commentLine = getLineOfLocalPositionFromLineMap(lineMap, comment.pos);

                if (commentLine >= lastCommentLine + 2) {
                    // There was a blank line between the last comment and this comment.  This
                    // comment is not part of the copyright comments.  Return what we have so
                    // far.
                    break;
                }
            }

            detachedComments.push(comment);
            lastComment = comment;
        }

        if (detachedComments.length) {
            // All comments look like they could have been part of the copyright header.  Make
            // sure there is at least one blank line between it and the node.  If not, it's not
            // a copyright header.
            const lastCommentLine = getLineOfLocalPositionFromLineMap(lineMap, last(detachedComments).end);
            const nodeLine = getLineOfLocalPositionFromLineMap(lineMap, skipTrivia(text, node.pos));
            if (nodeLine >= lastCommentLine + 2) {
                // Valid detachedComments
                emitNewLineBeforeLeadingComments(lineMap, writer, node, leadingComments);
                emitComments(text, lineMap, writer, detachedComments, /*leadingSeparator*/ false, /*trailingSeparator*/ true, newLine, writeComment);
                currentDetachedCommentInfo = { nodePos: node.pos, detachedCommentEndPos: last(detachedComments).end };
            }
        }
    }

    return currentDetachedCommentInfo;

    function isPinnedCommentLocal(comment: CommentRange) {
        return isPinnedComment(text, comment.pos);
    }
}

/** @internal */
export function writeCommentRange(text: string, lineMap: readonly number[], writer: EmitTextWriter, commentPos: number, commentEnd: number, newLine: string) {
    if (text.charCodeAt(commentPos + 1) === CharacterCodes.asterisk) {
        const firstCommentLineAndCharacter = computeLineAndCharacterOfPosition(lineMap, commentPos);
        const lineCount = lineMap.length;
        let firstCommentLineIndent: number | undefined;
        for (let pos = commentPos, currentLine = firstCommentLineAndCharacter.line; pos < commentEnd; currentLine++) {
            const nextLineStart = (currentLine + 1) === lineCount
                ? text.length + 1
                : lineMap[currentLine + 1];

            if (pos !== commentPos) {
                // If we are not emitting first line, we need to write the spaces to adjust the alignment
                if (firstCommentLineIndent === undefined) {
                    firstCommentLineIndent = calculateIndent(text, lineMap[firstCommentLineAndCharacter.line], commentPos);
                }

                // These are number of spaces writer is going to write at current indent
                const currentWriterIndentSpacing = writer.getIndent() * getIndentSize();

                // Number of spaces we want to be writing
                // eg: Assume writer indent
                // module m {
                //         /* starts at character 9 this is line 1
                //    * starts at character pos 4 line                        --1  = 8 - 8 + 3
                //   More left indented comment */                            --2  = 8 - 8 + 2
                //     class c { }
                // }
                // module m {
                //     /* this is line 1 -- Assume current writer indent 8
                //      * line                                                --3 = 8 - 4 + 5
                //            More right indented comment */                  --4 = 8 - 4 + 11
                //     class c { }
                // }
                const spacesToEmit = currentWriterIndentSpacing - firstCommentLineIndent + calculateIndent(text, pos, nextLineStart);
                if (spacesToEmit > 0) {
                    let numberOfSingleSpacesToEmit = spacesToEmit % getIndentSize();
                    const indentSizeSpaceString = getIndentString((spacesToEmit - numberOfSingleSpacesToEmit) / getIndentSize());

                    // Write indent size string ( in eg 1: = "", 2: "" , 3: string with 8 spaces 4: string with 12 spaces
                    writer.rawWrite(indentSizeSpaceString);

                    // Emit the single spaces (in eg: 1: 3 spaces, 2: 2 spaces, 3: 1 space, 4: 3 spaces)
                    while (numberOfSingleSpacesToEmit) {
                        writer.rawWrite(" ");
                        numberOfSingleSpacesToEmit--;
                    }
                }
                else {
                    // No spaces to emit write empty string
                    writer.rawWrite("");
                }
            }

            // Write the comment line text
            writeTrimmedCurrentLine(text, commentEnd, writer, newLine, pos, nextLineStart);

            pos = nextLineStart;
        }
    }
    else {
        // Single line comment of style //....
        writer.writeComment(text.substring(commentPos, commentEnd));
    }
}

function writeTrimmedCurrentLine(text: string, commentEnd: number, writer: EmitTextWriter, newLine: string, pos: number, nextLineStart: number) {
    const end = Math.min(commentEnd, nextLineStart - 1);
    const currentLineText = text.substring(pos, end).trim();
    if (currentLineText) {
        // trimmed forward and ending spaces text
        writer.writeComment(currentLineText);
        if (end !== commentEnd) {
            writer.writeLine();
        }
    }
    else {
        // Empty string - make sure we write empty line
        writer.rawWrite(newLine);
    }
}

function calculateIndent(text: string, pos: number, end: number) {
    let currentLineIndent = 0;
    for (; pos < end && isWhiteSpaceSingleLine(text.charCodeAt(pos)); pos++) {
        if (text.charCodeAt(pos) === CharacterCodes.tab) {
            // Tabs = TabSize = indent size and go to next tabStop
            currentLineIndent += getIndentSize() - (currentLineIndent % getIndentSize());
        }
        else {
            // Single space
            currentLineIndent++;
        }
    }

    return currentLineIndent;
}

/** @internal */
export function hasEffectiveModifiers(node: Node) {
    return getEffectiveModifierFlags(node) !== ModifierFlags.None;
}

/** @internal */
export function hasSyntacticModifiers(node: Node) {
    return getSyntacticModifierFlags(node) !== ModifierFlags.None;
}

/** @internal */
export function hasEffectiveModifier(node: Node, flags: ModifierFlags): boolean {
    return !!getSelectedEffectiveModifierFlags(node, flags);
}

/** @internal */
export function hasSyntacticModifier(node: Node, flags: ModifierFlags): boolean {
    return !!getSelectedSyntacticModifierFlags(node, flags);
}

/** @internal */
export function isStatic(node: Node) {
    // https://tc39.es/ecma262/#sec-static-semantics-isstatic
    return isClassElement(node) && hasStaticModifier(node) || isClassStaticBlockDeclaration(node);
}

/** @internal */
export function hasStaticModifier(node: Node): boolean {
    return hasSyntacticModifier(node, ModifierFlags.Static);
}

/** @internal */
export function hasOverrideModifier(node: Node): boolean {
    return hasEffectiveModifier(node, ModifierFlags.Override);
}

/** @internal */
export function hasAbstractModifier(node: Node): boolean {
    return hasSyntacticModifier(node, ModifierFlags.Abstract);
}

/** @internal */
export function hasAmbientModifier(node: Node): boolean {
    return hasSyntacticModifier(node, ModifierFlags.Ambient);
}

/** @internal */
export function hasAccessorModifier(node: Node): boolean {
    return hasSyntacticModifier(node, ModifierFlags.Accessor);
}

/** @internal */
export function hasEffectiveReadonlyModifier(node: Node): boolean {
    return hasEffectiveModifier(node, ModifierFlags.Readonly);
}

/** @internal */
export function hasDecorators(node: Node): boolean {
    return hasSyntacticModifier(node, ModifierFlags.Decorator);
}

/** @internal */
export function getSelectedEffectiveModifierFlags(node: Node, flags: ModifierFlags): ModifierFlags {
    return getEffectiveModifierFlags(node) & flags;
}

/** @internal */
export function getSelectedSyntacticModifierFlags(node: Node, flags: ModifierFlags): ModifierFlags {
    return getSyntacticModifierFlags(node) & flags;
}

function getModifierFlagsWorker(node: Node, includeJSDoc: boolean, alwaysIncludeJSDoc?: boolean): ModifierFlags {
    if (node.kind >= SyntaxKind.FirstToken && node.kind <= SyntaxKind.LastToken) {
        return ModifierFlags.None;
    }

    if (!(node.modifierFlagsCache & ModifierFlags.HasComputedFlags)) {
        node.modifierFlagsCache = getSyntacticModifierFlagsNoCache(node) | ModifierFlags.HasComputedFlags;
    }

    if (alwaysIncludeJSDoc || includeJSDoc && isInJSFile(node)) {
        if (!(node.modifierFlagsCache & ModifierFlags.HasComputedJSDocModifiers) && node.parent) {
            node.modifierFlagsCache |= getRawJSDocModifierFlagsNoCache(node) | ModifierFlags.HasComputedJSDocModifiers;
        }
        return selectEffectiveModifierFlags(node.modifierFlagsCache);
    }

    return selectSyntacticModifierFlags(node.modifierFlagsCache);
}

/**
 * Gets the effective ModifierFlags for the provided node, including JSDoc modifiers. The modifiers will be cached on the node to improve performance.
 *
 * NOTE: This function may use `parent` pointers.
 *
 * @internal
 */
export function getEffectiveModifierFlags(node: Node): ModifierFlags {
    return getModifierFlagsWorker(node, /*includeJSDoc*/ true);
}

/** @internal */
export function getEffectiveModifierFlagsAlwaysIncludeJSDoc(node: Node): ModifierFlags {
    return getModifierFlagsWorker(node, /*includeJSDoc*/ true, /*alwaysIncludeJSDoc*/ true);
}

/**
 * Gets the ModifierFlags for syntactic modifiers on the provided node. The modifiers will be cached on the node to improve performance.
 *
 * NOTE: This function does not use `parent` pointers and will not include modifiers from JSDoc.
 *
 * @internal
 */
export function getSyntacticModifierFlags(node: Node): ModifierFlags {
    return getModifierFlagsWorker(node, /*includeJSDoc*/ false);
}

function getRawJSDocModifierFlagsNoCache(node: Node): ModifierFlags {
    let flags = ModifierFlags.None;
    if (!!node.parent && !isParameter(node)) {
        if (isInJSFile(node)) {
            if (getJSDocPublicTagNoCache(node)) flags |= ModifierFlags.JSDocPublic;
            if (getJSDocPrivateTagNoCache(node)) flags |= ModifierFlags.JSDocPrivate;
            if (getJSDocProtectedTagNoCache(node)) flags |= ModifierFlags.JSDocProtected;
            if (getJSDocReadonlyTagNoCache(node)) flags |= ModifierFlags.JSDocReadonly;
            if (getJSDocOverrideTagNoCache(node)) flags |= ModifierFlags.JSDocOverride;
        }
        if (getJSDocDeprecatedTagNoCache(node)) flags |= ModifierFlags.Deprecated;
    }

    return flags;
}

function selectSyntacticModifierFlags(flags: ModifierFlags) {
    return flags & ModifierFlags.SyntacticModifiers;
}

function selectEffectiveModifierFlags(flags: ModifierFlags) {
    return (flags & ModifierFlags.NonCacheOnlyModifiers) |
        ((flags & ModifierFlags.JSDocCacheOnlyModifiers) >>> 23); // shift ModifierFlags.JSDoc* to match ModifierFlags.*
}

function getJSDocModifierFlagsNoCache(node: Node): ModifierFlags {
    return selectEffectiveModifierFlags(getRawJSDocModifierFlagsNoCache(node));
}

/**
 * Gets the effective ModifierFlags for the provided node, including JSDoc modifiers. The modifier flags cache on the node is ignored.
 *
 * NOTE: This function may use `parent` pointers.
 *
 * @internal
 */
export function getEffectiveModifierFlagsNoCache(node: Node): ModifierFlags {
    return getSyntacticModifierFlagsNoCache(node) | getJSDocModifierFlagsNoCache(node);
}

/**
 * Gets the ModifierFlags for syntactic modifiers on the provided node. The modifier flags cache on the node is ignored.
 *
 * NOTE: This function does not use `parent` pointers and will not include modifiers from JSDoc.
 *
 * @internal
 */
export function getSyntacticModifierFlagsNoCache(node: Node): ModifierFlags {
    let flags = canHaveModifiers(node) ? modifiersToFlags(node.modifiers) : ModifierFlags.None;
    if (node.flags & NodeFlags.NestedNamespace || node.kind === SyntaxKind.Identifier && node.flags & NodeFlags.IdentifierIsInJSDocNamespace) {
        flags |= ModifierFlags.Export;
    }
    return flags;
}

/** @internal */
export function modifiersToFlags(modifiers: readonly ModifierLike[] | undefined) {
    let flags = ModifierFlags.None;
    if (modifiers) {
        for (const modifier of modifiers) {
            flags |= modifierToFlag(modifier.kind);
        }
    }
    return flags;
}

/** @internal */
export function modifierToFlag(token: SyntaxKind): ModifierFlags {
    switch (token) {
        case SyntaxKind.StaticKeyword:
            return ModifierFlags.Static;
        case SyntaxKind.PublicKeyword:
            return ModifierFlags.Public;
        case SyntaxKind.ProtectedKeyword:
            return ModifierFlags.Protected;
        case SyntaxKind.PrivateKeyword:
            return ModifierFlags.Private;
        case SyntaxKind.AbstractKeyword:
            return ModifierFlags.Abstract;
        case SyntaxKind.AccessorKeyword:
            return ModifierFlags.Accessor;
        case SyntaxKind.ExportKeyword:
            return ModifierFlags.Export;
        case SyntaxKind.DeclareKeyword:
            return ModifierFlags.Ambient;
        case SyntaxKind.ConstKeyword:
            return ModifierFlags.Const;
        case SyntaxKind.DefaultKeyword:
            return ModifierFlags.Default;
        case SyntaxKind.AsyncKeyword:
            return ModifierFlags.Async;
        case SyntaxKind.ReadonlyKeyword:
            return ModifierFlags.Readonly;
        case SyntaxKind.OverrideKeyword:
            return ModifierFlags.Override;
        case SyntaxKind.InKeyword:
            return ModifierFlags.In;
        case SyntaxKind.OutKeyword:
            return ModifierFlags.Out;
        case SyntaxKind.Decorator:
            return ModifierFlags.Decorator;
    }
    return ModifierFlags.None;
}

function isBinaryLogicalOperator(token: SyntaxKind): boolean {
    return token === SyntaxKind.BarBarToken || token === SyntaxKind.AmpersandAmpersandToken;
}

/** @internal */
export function isLogicalOperator(token: SyntaxKind): boolean {
    return isBinaryLogicalOperator(token) || token === SyntaxKind.ExclamationToken;
}

/** @internal */
export function isLogicalOrCoalescingAssignmentOperator(token: SyntaxKind): token is LogicalOrCoalescingAssignmentOperator {
    return token === SyntaxKind.BarBarEqualsToken
        || token === SyntaxKind.AmpersandAmpersandEqualsToken
        || token === SyntaxKind.QuestionQuestionEqualsToken;
}

/** @internal */
export function isLogicalOrCoalescingAssignmentExpression(expr: Node): expr is AssignmentExpression<Token<LogicalOrCoalescingAssignmentOperator>> {
    return isBinaryExpression(expr) && isLogicalOrCoalescingAssignmentOperator(expr.operatorToken.kind);
}

/** @internal */
export function isLogicalOrCoalescingBinaryOperator(token: SyntaxKind): token is LogicalOperator | SyntaxKind.QuestionQuestionToken {
    return isBinaryLogicalOperator(token) || token === SyntaxKind.QuestionQuestionToken;
}

/** @internal */
export function isLogicalOrCoalescingBinaryExpression(expr: Node): expr is BinaryExpression {
    return isBinaryExpression(expr) && isLogicalOrCoalescingBinaryOperator(expr.operatorToken.kind);
}

/** @internal */
export function isAssignmentOperator(token: SyntaxKind): boolean {
    return token >= SyntaxKind.FirstAssignment && token <= SyntaxKind.LastAssignment;
}

/**
 * Get `C` given `N` if `N` is in the position `class C extends N` where `N` is an ExpressionWithTypeArguments.
 *
 * @internal
 */
export function tryGetClassExtendingExpressionWithTypeArguments(node: Node): ClassLikeDeclaration | undefined {
    const cls = tryGetClassImplementingOrExtendingExpressionWithTypeArguments(node);
    return cls && !cls.isImplements ? cls.class : undefined;
}

/** @internal */
export interface ClassImplementingOrExtendingExpressionWithTypeArguments {
    readonly class: ClassLikeDeclaration;
    readonly isImplements: boolean;
}
/** @internal */
export function tryGetClassImplementingOrExtendingExpressionWithTypeArguments(node: Node): ClassImplementingOrExtendingExpressionWithTypeArguments | undefined {
    if (isExpressionWithTypeArguments(node)) {
        if (isHeritageClause(node.parent) && isClassLike(node.parent.parent)) {
            return { class: node.parent.parent, isImplements: node.parent.token === SyntaxKind.ImplementsKeyword };
        }
        if (isJSDocAugmentsTag(node.parent)) {
            const host = getEffectiveJSDocHost(node.parent);
            if (host && isClassLike(host)) {
                return { class: host, isImplements: false };
            }
        }
    }
    return undefined;
}

/** @internal */
export function isAssignmentExpression(node: Node, excludeCompoundAssignment: true): node is AssignmentExpression<EqualsToken>;
/** @internal */
export function isAssignmentExpression(node: Node, excludeCompoundAssignment?: false): node is AssignmentExpression<AssignmentOperatorToken>;
/** @internal */
export function isAssignmentExpression(node: Node, excludeCompoundAssignment?: boolean): node is AssignmentExpression<AssignmentOperatorToken> {
    return isBinaryExpression(node)
        && (excludeCompoundAssignment
            ? node.operatorToken.kind === SyntaxKind.EqualsToken
            : isAssignmentOperator(node.operatorToken.kind))
        && isLeftHandSideExpression(node.left);
}

/** @internal */
export function isLeftHandSideOfAssignment(node: Node) {
    return isAssignmentExpression(node.parent) && node.parent.left === node;
}
/** @internal */
export function isDestructuringAssignment(node: Node): node is DestructuringAssignment {
    if (isAssignmentExpression(node, /*excludeCompoundAssignment*/ true)) {
        const kind = node.left.kind;
        return kind === SyntaxKind.ObjectLiteralExpression
            || kind === SyntaxKind.ArrayLiteralExpression;
    }

    return false;
}

/** @internal */
export function isExpressionWithTypeArgumentsInClassExtendsClause(node: Node): node is ExpressionWithTypeArguments {
    return tryGetClassExtendingExpressionWithTypeArguments(node) !== undefined;
}

/** @internal */
export function isEntityNameExpression(node: Node): node is EntityNameExpression {
    return node.kind === SyntaxKind.Identifier || isPropertyAccessEntityNameExpression(node);
}

/** @internal */
export function getFirstIdentifier(node: EntityNameOrEntityNameExpression): Identifier {
    switch (node.kind) {
        case SyntaxKind.Identifier:
            return node;
        case SyntaxKind.QualifiedName:
            do {
                node = node.left;
            }
            while (node.kind !== SyntaxKind.Identifier);
            return node;
        case SyntaxKind.PropertyAccessExpression:
            do {
                node = node.expression;
            }
            while (node.kind !== SyntaxKind.Identifier);
            return node;
    }
}

/** @internal */
export function isDottedName(node: Expression): boolean {
    return node.kind === SyntaxKind.Identifier
        || node.kind === SyntaxKind.ThisKeyword
        || node.kind === SyntaxKind.SuperKeyword
        || node.kind === SyntaxKind.MetaProperty
        || node.kind === SyntaxKind.PropertyAccessExpression && isDottedName((node as PropertyAccessExpression).expression)
        || node.kind === SyntaxKind.ParenthesizedExpression && isDottedName((node as ParenthesizedExpression).expression);
}

/** @internal */
export function isPropertyAccessEntityNameExpression(node: Node): node is PropertyAccessEntityNameExpression {
    return isPropertyAccessExpression(node) && isIdentifier(node.name) && isEntityNameExpression(node.expression);
}

/** @internal */
export function tryGetPropertyAccessOrIdentifierToString(expr: Expression | JsxTagNameExpression): string | undefined {
    if (isPropertyAccessExpression(expr)) {
        const baseStr = tryGetPropertyAccessOrIdentifierToString(expr.expression);
        if (baseStr !== undefined) {
            return baseStr + "." + entityNameToString(expr.name);
        }
    }
    else if (isElementAccessExpression(expr)) {
        const baseStr = tryGetPropertyAccessOrIdentifierToString(expr.expression);
        if (baseStr !== undefined && isPropertyName(expr.argumentExpression)) {
            return baseStr + "." + getPropertyNameForPropertyNameNode(expr.argumentExpression);
        }
    }
    else if (isIdentifier(expr)) {
        return unescapeLeadingUnderscores(expr.escapedText);
    }
    else if (isJsxNamespacedName(expr)) {
        return getTextOfJsxNamespacedName(expr);
    }
    return undefined;
}

/** @internal */
export function isPrototypeAccess(node: Node): node is BindableStaticAccessExpression {
    return isBindableStaticAccessExpression(node) && getElementOrPropertyAccessName(node) === "prototype";
}

/** @internal */
export function isRightSideOfQualifiedNameOrPropertyAccess(node: Node) {
    return (node.parent.kind === SyntaxKind.QualifiedName && (node.parent as QualifiedName).right === node) ||
        (node.parent.kind === SyntaxKind.PropertyAccessExpression && (node.parent as PropertyAccessExpression).name === node) ||
        (node.parent.kind === SyntaxKind.MetaProperty && (node.parent as MetaProperty).name === node);
}

/** @internal */
export function isRightSideOfAccessExpression(node: Node) {
    return !!node.parent && (isPropertyAccessExpression(node.parent) && node.parent.name === node
        || isElementAccessExpression(node.parent) && node.parent.argumentExpression === node);
}

/** @internal */
export function isRightSideOfQualifiedNameOrPropertyAccessOrJSDocMemberName(node: Node) {
    return isQualifiedName(node.parent) && node.parent.right === node
        || isPropertyAccessExpression(node.parent) && node.parent.name === node
        || isJSDocMemberName(node.parent) && node.parent.right === node;
}
/** @internal */
export function isInstanceOfExpression(node: Node): node is InstanceofExpression {
    return isBinaryExpression(node) && node.operatorToken.kind === SyntaxKind.InstanceOfKeyword;
}

/** @internal */
export function isRightSideOfInstanceofExpression(node: Node) {
    return isInstanceOfExpression(node.parent) && node === node.parent.right;
}

/** @internal */
export function isEmptyObjectLiteral(expression: Node): boolean {
    return expression.kind === SyntaxKind.ObjectLiteralExpression &&
        (expression as ObjectLiteralExpression).properties.length === 0;
}

/** @internal */
export function isEmptyArrayLiteral(expression: Node): boolean {
    return expression.kind === SyntaxKind.ArrayLiteralExpression &&
        (expression as ArrayLiteralExpression).elements.length === 0;
}

/** @internal */
export function getLocalSymbolForExportDefault(symbol: Symbol) {
    if (!isExportDefaultSymbol(symbol) || !symbol.declarations) return undefined;
    for (const decl of symbol.declarations) {
        if (decl.localSymbol) return decl.localSymbol;
    }
    return undefined;
}

function isExportDefaultSymbol(symbol: Symbol): boolean {
    return symbol && length(symbol.declarations) > 0 && hasSyntacticModifier(symbol.declarations![0], ModifierFlags.Default);
}

/**
 * Return ".ts", ".d.ts", or ".tsx", if that is the extension.
 *
 * @internal
 */
export function tryExtractTSExtension(fileName: string): string | undefined {
    return find(supportedTSExtensionsForExtractExtension, extension => fileExtensionIs(fileName, extension));
}
/**
 * Replace each instance of non-ascii characters by one, two, three, or four escape sequences
 * representing the UTF-8 encoding of the character, and return the expanded char code list.
 */
function getExpandedCharCodes(input: string): number[] {
    const output: number[] = [];
    const length = input.length;

    for (let i = 0; i < length; i++) {
        const charCode = input.charCodeAt(i);

        // handle utf8
        if (charCode < 0x80) {
            output.push(charCode);
        }
        else if (charCode < 0x800) {
            output.push((charCode >> 6) | 0B11000000);
            output.push((charCode & 0B00111111) | 0B10000000);
        }
        else if (charCode < 0x10000) {
            output.push((charCode >> 12) | 0B11100000);
            output.push(((charCode >> 6) & 0B00111111) | 0B10000000);
            output.push((charCode & 0B00111111) | 0B10000000);
        }
        else if (charCode < 0x20000) {
            output.push((charCode >> 18) | 0B11110000);
            output.push(((charCode >> 12) & 0B00111111) | 0B10000000);
            output.push(((charCode >> 6) & 0B00111111) | 0B10000000);
            output.push((charCode & 0B00111111) | 0B10000000);
        }
        else {
            Debug.assert(false, "Unexpected code point");
        }
    }

    return output;
}

const base64Digits = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=";

/**
 * Converts a string to a base-64 encoded ASCII string.
 *
 * @internal
 */
export function convertToBase64(input: string): string {
    let result = "";
    const charCodes = getExpandedCharCodes(input);
    let i = 0;
    const length = charCodes.length;
    let byte1: number, byte2: number, byte3: number, byte4: number;

    while (i < length) {
        // Convert every 6-bits in the input 3 character points
        // into a base64 digit
        byte1 = charCodes[i] >> 2;
        byte2 = (charCodes[i] & 0B00000011) << 4 | charCodes[i + 1] >> 4;
        byte3 = (charCodes[i + 1] & 0B00001111) << 2 | charCodes[i + 2] >> 6;
        byte4 = charCodes[i + 2] & 0B00111111;

        // We are out of characters in the input, set the extra
        // digits to 64 (padding character).
        if (i + 1 >= length) {
            byte3 = byte4 = 64;
        }
        else if (i + 2 >= length) {
            byte4 = 64;
        }

        // Write to the output
        result += base64Digits.charAt(byte1) + base64Digits.charAt(byte2) + base64Digits.charAt(byte3) + base64Digits.charAt(byte4);

        i += 3;
    }

    return result;
}

function getStringFromExpandedCharCodes(codes: number[]): string {
    let output = "";
    let i = 0;
    const length = codes.length;
    while (i < length) {
        const charCode = codes[i];

        if (charCode < 0x80) {
            output += String.fromCharCode(charCode);
            i++;
        }
        else if ((charCode & 0B11000000) === 0B11000000) {
            let value = charCode & 0B00111111;
            i++;
            let nextCode: number = codes[i];
            while ((nextCode & 0B11000000) === 0B10000000) {
                value = (value << 6) | (nextCode & 0B00111111);
                i++;
                nextCode = codes[i];
            }
            // `value` may be greater than 10FFFF (the maximum unicode codepoint) - JS will just make this into an invalid character for us
            output += String.fromCharCode(value);
        }
        else {
            // We don't want to kill the process when decoding fails (due to a following char byte not
            // following a leading char), so we just print the (bad) value
            output += String.fromCharCode(charCode);
            i++;
        }
    }
    return output;
}

/** @internal */
export function base64encode(host: { base64encode?(input: string): string; } | undefined, input: string): string {
    if (host && host.base64encode) {
        return host.base64encode(input);
    }
    return convertToBase64(input);
}

/** @internal */
export function base64decode(host: { base64decode?(input: string): string; } | undefined, input: string): string {
    if (host && host.base64decode) {
        return host.base64decode(input);
    }
    const length = input.length;
    const expandedCharCodes: number[] = [];
    let i = 0;
    while (i < length) {
        // Stop decoding once padding characters are present
        if (input.charCodeAt(i) === base64Digits.charCodeAt(64)) {
            break;
        }
        // convert 4 input digits into three characters, ignoring padding characters at the end
        const ch1 = base64Digits.indexOf(input[i]);
        const ch2 = base64Digits.indexOf(input[i + 1]);
        const ch3 = base64Digits.indexOf(input[i + 2]);
        const ch4 = base64Digits.indexOf(input[i + 3]);

        const code1 = ((ch1 & 0B00111111) << 2) | ((ch2 >> 4) & 0B00000011);
        const code2 = ((ch2 & 0B00001111) << 4) | ((ch3 >> 2) & 0B00001111);
        const code3 = ((ch3 & 0B00000011) << 6) | (ch4 & 0B00111111);

        if (code2 === 0 && ch3 !== 0) { // code2 decoded to zero, but ch3 was padding - elide code2 and code3
            expandedCharCodes.push(code1);
        }
        else if (code3 === 0 && ch4 !== 0) { // code3 decoded to zero, but ch4 was padding, elide code3
            expandedCharCodes.push(code1, code2);
        }
        else {
            expandedCharCodes.push(code1, code2, code3);
        }
        i += 4;
    }
    return getStringFromExpandedCharCodes(expandedCharCodes);
}

/** @internal */
export function readJsonOrUndefined(path: string, hostOrText: { readFile(fileName: string): string | undefined; } | string): object | undefined {
    const jsonText = isString(hostOrText) ? hostOrText : hostOrText.readFile(path);
    if (!jsonText) return undefined;
    // gracefully handle if readFile fails or returns not JSON
    const result = parseConfigFileTextToJson(path, jsonText);
    return !result.error ? result.config : undefined;
}

/** @internal */
export function readJson(path: string, host: { readFile(fileName: string): string | undefined; }): object {
    return readJsonOrUndefined(path, host) || {};
}

/** @internal */
export function tryParseJson(text: string) {
    try {
        return JSON.parse(text);
    }
    catch {
        return undefined;
    }
}

/** @internal */
export function directoryProbablyExists(directoryName: string, host: { directoryExists?: (directoryName: string) => boolean; }): boolean {
    // if host does not support 'directoryExists' assume that directory will exist
    return !host.directoryExists || host.directoryExists(directoryName);
}

const carriageReturnLineFeed = "\r\n";
const lineFeed = "\n";
/** @internal */
export function getNewLineCharacter(options: CompilerOptions | PrinterOptions): string {
    switch (options.newLine) {
        case NewLineKind.CarriageReturnLineFeed:
            return carriageReturnLineFeed;
        case NewLineKind.LineFeed:
        case undefined:
            return lineFeed;
    }
}

/**
 * Creates a new TextRange from the provided pos and end.
 *
 * @param pos The start position.
 * @param end The end position.
 *
 * @internal
 */
export function createRange(pos: number, end: number = pos): TextRange {
    Debug.assert(end >= pos || end === -1);
    return { pos, end };
}

/**
 * Creates a new TextRange from a provided range with a new end position.
 *
 * @param range A TextRange.
 * @param end The new end position.
 *
 * @internal
 */
export function moveRangeEnd(range: TextRange, end: number): TextRange {
    return createRange(range.pos, end);
}

/**
 * Creates a new TextRange from a provided range with a new start position.
 *
 * @param range A TextRange.
 * @param pos The new Start position.
 *
 * @internal
 */
export function moveRangePos(range: TextRange, pos: number): TextRange {
    return createRange(pos, range.end);
}

/**
 * Moves the start position of a range past any decorators.
 *
 * @internal
 */
export function moveRangePastDecorators(node: Node): TextRange {
    const lastDecorator = canHaveModifiers(node) ? findLast(node.modifiers, isDecorator) : undefined;
    return lastDecorator && !positionIsSynthesized(lastDecorator.end)
        ? moveRangePos(node, lastDecorator.end)
        : node;
}

/**
 * Moves the start position of a range past any decorators or modifiers.
 *
 * @internal
 */
export function moveRangePastModifiers(node: Node): TextRange {
    if (isPropertyDeclaration(node) || isMethodDeclaration(node)) {
        return moveRangePos(node, node.name.pos);
    }

    const lastModifier = canHaveModifiers(node) ? lastOrUndefined(node.modifiers) : undefined;
    return lastModifier && !positionIsSynthesized(lastModifier.end)
        ? moveRangePos(node, lastModifier.end)
        : moveRangePastDecorators(node);
}

/**
 * Determines whether a TextRange has the same start and end positions.
 *
 * @param range A TextRange.
 *
 * @internal
 */
export function isCollapsedRange(range: TextRange) {
    return range.pos === range.end;
}

/**
 * Creates a new TextRange for a token at the provides start position.
 *
 * @param pos The start position.
 * @param token The token.
 *
 * @internal
 */
export function createTokenRange(pos: number, token: SyntaxKind): TextRange {
    return createRange(pos, pos + tokenToString(token)!.length);
}

/** @internal */
export function rangeIsOnSingleLine(range: TextRange, sourceFile: SourceFile) {
    return rangeStartIsOnSameLineAsRangeEnd(range, range, sourceFile);
}

/** @internal */
export function rangeStartPositionsAreOnSameLine(range1: TextRange, range2: TextRange, sourceFile: SourceFile) {
    return positionsAreOnSameLine(
        getStartPositionOfRange(range1, sourceFile, /*includeComments*/ false),
        getStartPositionOfRange(range2, sourceFile, /*includeComments*/ false),
        sourceFile,
    );
}

/** @internal */
export function rangeEndPositionsAreOnSameLine(range1: TextRange, range2: TextRange, sourceFile: SourceFile) {
    return positionsAreOnSameLine(range1.end, range2.end, sourceFile);
}

/** @internal */
export function rangeStartIsOnSameLineAsRangeEnd(range1: TextRange, range2: TextRange, sourceFile: SourceFile) {
    return positionsAreOnSameLine(getStartPositionOfRange(range1, sourceFile, /*includeComments*/ false), range2.end, sourceFile);
}

/** @internal */
export function rangeEndIsOnSameLineAsRangeStart(range1: TextRange, range2: TextRange, sourceFile: SourceFile) {
    return positionsAreOnSameLine(range1.end, getStartPositionOfRange(range2, sourceFile, /*includeComments*/ false), sourceFile);
}

/** @internal */
export function getLinesBetweenRangeEndAndRangeStart(range1: TextRange, range2: TextRange, sourceFile: SourceFile, includeSecondRangeComments: boolean) {
    const range2Start = getStartPositionOfRange(range2, sourceFile, includeSecondRangeComments);
    return getLinesBetweenPositions(sourceFile, range1.end, range2Start);
}

/** @internal */
export function getLinesBetweenRangeEndPositions(range1: TextRange, range2: TextRange, sourceFile: SourceFile) {
    return getLinesBetweenPositions(sourceFile, range1.end, range2.end);
}

/** @internal */
export function isNodeArrayMultiLine(list: NodeArray<Node>, sourceFile: SourceFile): boolean {
    return !positionsAreOnSameLine(list.pos, list.end, sourceFile);
}

/** @internal */
export function positionsAreOnSameLine(pos1: number, pos2: number, sourceFile: SourceFile) {
    return getLinesBetweenPositions(sourceFile, pos1, pos2) === 0;
}

/** @internal */
export function getStartPositionOfRange(range: TextRange, sourceFile: SourceFile, includeComments: boolean) {
    return positionIsSynthesized(range.pos) ? -1 : skipTrivia(sourceFile.text, range.pos, /*stopAfterLineBreak*/ false, includeComments);
}

/** @internal */
export function getLinesBetweenPositionAndPrecedingNonWhitespaceCharacter(pos: number, stopPos: number, sourceFile: SourceFile, includeComments?: boolean) {
    const startPos = skipTrivia(sourceFile.text, pos, /*stopAfterLineBreak*/ false, includeComments);
    const prevPos = getPreviousNonWhitespacePosition(startPos, stopPos, sourceFile);
    return getLinesBetweenPositions(sourceFile, prevPos ?? stopPos, startPos);
}

/** @internal */
export function getLinesBetweenPositionAndNextNonWhitespaceCharacter(pos: number, stopPos: number, sourceFile: SourceFile, includeComments?: boolean) {
    const nextPos = skipTrivia(sourceFile.text, pos, /*stopAfterLineBreak*/ false, includeComments);
    return getLinesBetweenPositions(sourceFile, pos, Math.min(stopPos, nextPos));
}

function getPreviousNonWhitespacePosition(pos: number, stopPos = 0, sourceFile: SourceFile) {
    while (pos-- > stopPos) {
        if (!isWhiteSpaceLike(sourceFile.text.charCodeAt(pos))) {
            return pos;
        }
    }
}

/**
 * Determines whether a name was originally the declaration name of an enum or namespace
 * declaration.
 *
 * @internal
 */
export function isDeclarationNameOfEnumOrNamespace(node: Identifier) {
    const parseNode = getParseTreeNode(node);
    if (parseNode) {
        switch (parseNode.parent.kind) {
            case SyntaxKind.EnumDeclaration:
            case SyntaxKind.ModuleDeclaration:
                return parseNode === (parseNode.parent as EnumDeclaration | ModuleDeclaration).name;
        }
    }
    return false;
}

/** @internal */
export function getInitializedVariables(node: VariableDeclarationList) {
    return filter(node.declarations, isInitializedVariable);
}

/** @internal */
export function isInitializedVariable(node: Node): node is InitializedVariableDeclaration {
    return isVariableDeclaration(node) && node.initializer !== undefined;
}

/** @internal */
export function isWatchSet(options: CompilerOptions) {
    // Firefox has Object.prototype.watch
    return options.watch && hasProperty(options, "watch");
}

/** @internal */
export function closeFileWatcher(watcher: FileWatcher) {
    watcher.close();
}

/** @internal */
export function getCheckFlags(symbol: Symbol): CheckFlags {
    return symbol.flags & SymbolFlags.Transient ? (symbol as TransientSymbol).links.checkFlags : 0;
}

/** @internal */
export function getDeclarationModifierFlagsFromSymbol(s: Symbol, isWrite = false): ModifierFlags {
    if (s.valueDeclaration) {
        const declaration = (isWrite && s.declarations && find(s.declarations, isSetAccessorDeclaration))
            || (s.flags & SymbolFlags.GetAccessor && find(s.declarations, isGetAccessorDeclaration)) || s.valueDeclaration;
        const flags = getCombinedModifierFlags(declaration);
        return s.parent && s.parent.flags & SymbolFlags.Class ? flags : flags & ~ModifierFlags.AccessibilityModifier;
    }
    if (getCheckFlags(s) & CheckFlags.Synthetic) {
        // NOTE: potentially unchecked cast to TransientSymbol
        const checkFlags = (s as TransientSymbol).links.checkFlags;
        const accessModifier = checkFlags & CheckFlags.ContainsPrivate ? ModifierFlags.Private :
            checkFlags & CheckFlags.ContainsPublic ? ModifierFlags.Public :
            ModifierFlags.Protected;
        const staticModifier = checkFlags & CheckFlags.ContainsStatic ? ModifierFlags.Static : 0;
        return accessModifier | staticModifier;
    }
    if (s.flags & SymbolFlags.Prototype) {
        return ModifierFlags.Public | ModifierFlags.Static;
    }
    return 0;
}

/** @internal */
export function skipAlias(symbol: Symbol, checker: TypeChecker) {
    return symbol.flags & SymbolFlags.Alias ? checker.getAliasedSymbol(symbol) : symbol;
}

/**
 * See comment on `declareModuleMember` in `binder.ts`.
 *
 * @internal
 */
export function getCombinedLocalAndExportSymbolFlags(symbol: Symbol): SymbolFlags {
    return symbol.exportSymbol ? symbol.exportSymbol.flags | symbol.flags : symbol.flags;
}

/** @internal */
export function isWriteOnlyAccess(node: Node) {
    return accessKind(node) === AccessKind.Write;
}

/** @internal */
export function isWriteAccess(node: Node) {
    return accessKind(node) !== AccessKind.Read;
}

const enum AccessKind {
    /** Only reads from a variable. */
    Read,
    /** Only writes to a variable without ever reading it. E.g.: `x=1;`. */
    Write,
    /** Reads from and writes to a variable. E.g.: `f(x++);`, `x/=1`. */
    ReadWrite,
}
function accessKind(node: Node): AccessKind {
    const { parent } = node;

    switch (parent?.kind) {
        case SyntaxKind.ParenthesizedExpression:
            return accessKind(parent);
        case SyntaxKind.PostfixUnaryExpression:
        case SyntaxKind.PrefixUnaryExpression:
            const { operator } = parent as PrefixUnaryExpression | PostfixUnaryExpression;
            return operator === SyntaxKind.PlusPlusToken || operator === SyntaxKind.MinusMinusToken ? AccessKind.ReadWrite : AccessKind.Read;
        case SyntaxKind.BinaryExpression:
            const { left, operatorToken } = parent as BinaryExpression;
            return left === node && isAssignmentOperator(operatorToken.kind) ?
                operatorToken.kind === SyntaxKind.EqualsToken ? AccessKind.Write : AccessKind.ReadWrite
                : AccessKind.Read;
        case SyntaxKind.PropertyAccessExpression:
            return (parent as PropertyAccessExpression).name !== node ? AccessKind.Read : accessKind(parent);
        case SyntaxKind.PropertyAssignment: {
            const parentAccess = accessKind(parent.parent);
            // In `({ x: varname }) = { x: 1 }`, the left `x` is a read, the right `x` is a write.
            return node === (parent as PropertyAssignment).name ? reverseAccessKind(parentAccess) : parentAccess;
        }
        case SyntaxKind.ShorthandPropertyAssignment:
            // Assume it's the local variable being accessed, since we don't check public properties for --noUnusedLocals.
            return node === (parent as ShorthandPropertyAssignment).objectAssignmentInitializer ? AccessKind.Read : accessKind(parent.parent);
        case SyntaxKind.ArrayLiteralExpression:
            return accessKind(parent);
        default:
            return AccessKind.Read;
    }
}
function reverseAccessKind(a: AccessKind): AccessKind {
    switch (a) {
        case AccessKind.Read:
            return AccessKind.Write;
        case AccessKind.Write:
            return AccessKind.Read;
        case AccessKind.ReadWrite:
            return AccessKind.ReadWrite;
        default:
            return Debug.assertNever(a);
    }
}

/** @internal */
export function compareDataObjects(dst: any, src: any): boolean {
    if (!dst || !src || Object.keys(dst).length !== Object.keys(src).length) {
        return false;
    }

    for (const e in dst) {
        if (typeof dst[e] === "object") {
            if (!compareDataObjects(dst[e], src[e])) {
                return false;
            }
        }
        else if (typeof dst[e] !== "function") {
            if (dst[e] !== src[e]) {
                return false;
            }
        }
    }
    return true;
}

/**
 * clears already present map by calling onDeleteExistingValue callback before deleting that key/value
 *
 * @internal
 */
export function clearMap<K, T>(map: { forEach: Map<K, T>["forEach"]; clear: Map<K, T>["clear"]; }, onDeleteValue: (valueInMap: T, key: K) => void) {
    // Remove all
    map.forEach(onDeleteValue);
    map.clear();
}

/** @internal */
export interface MutateMapSkippingNewValuesDelete<K, T> {
    onDeleteValue(existingValue: T, key: K): void;
}

/** @internal */
export interface MutateMapSkippingNewValuesOptions<K, T, U> extends MutateMapSkippingNewValuesDelete<K, T> {
    /**
     * If present this is called with the key when there is value for that key both in new map as well as existing map provided
     * Caller can then decide to update or remove this key.
     * If the key is removed, caller will get callback of createNewValue for that key.
     * If this callback is not provided, the value of such keys is not updated.
     */
    onExistingValue?(existingValue: T, valueInNewMap: U, key: K): void;
}

/**
 * Mutates the map with newMap such that keys in map will be same as newMap.
 *
 * @internal
 */
export function mutateMapSkippingNewValues<K, T>(
    map: Map<K, T>,
    newMap: ReadonlySet<K> | undefined,
    options: MutateMapSkippingNewValuesDelete<K, T>,
): void;
/** @internal */
export function mutateMapSkippingNewValues<K, T, U>(
    map: Map<K, T>,
    newMap: ReadonlyMap<K, U> | undefined,
    options: MutateMapSkippingNewValuesOptions<K, T, U>,
): void;
export function mutateMapSkippingNewValues<K, T, U>(
    map: Map<K, T>,
    newMap: ReadonlyMap<K, U> | ReadonlySet<K> | undefined,
    options: MutateMapSkippingNewValuesOptions<K, T, U>,
) {
    const { onDeleteValue, onExistingValue } = options;
    // Needs update
    map.forEach((existingValue, key) => {
        // Not present any more in new map, remove it
        if (!newMap?.has(key)) {
            map.delete(key);
            onDeleteValue(existingValue, key);
        }
        // If present notify about existing values
        else if (onExistingValue) {
            onExistingValue(existingValue, (newMap as Map<K, U>).get?.(key)!, key);
        }
    });
}

/** @internal */
export interface MutateMapOptionsCreate<K, T, U> {
    createNewValue(key: K, valueInNewMap: U): T;
}

/** @internal */
export interface MutateMapWithNewSetOptions<K, T> extends MutateMapSkippingNewValuesDelete<K, T>, MutateMapOptionsCreate<K, T, K> {
}

/** @internal */
export interface MutateMapOptions<K, T, U> extends MutateMapSkippingNewValuesOptions<K, T, U>, MutateMapOptionsCreate<K, T, U> {
}

/**
 * Mutates the map with newMap such that keys in map will be same as newMap.
 *
 * @internal
 */
export function mutateMap<K, T>(map: Map<K, T>, newMap: ReadonlySet<K> | undefined, options: MutateMapWithNewSetOptions<K, T>): void;
/** @internal */
export function mutateMap<K, T, U>(map: Map<K, T>, newMap: ReadonlyMap<K, U> | undefined, options: MutateMapOptions<K, T, U>): void;
export function mutateMap<K, T, U>(map: Map<K, T>, newMap: ReadonlyMap<K, U> | ReadonlySet<K> | undefined, options: MutateMapOptions<K, T, U>) {
    // Needs update
    mutateMapSkippingNewValues(map, newMap as ReadonlyMap<K, U>, options);

    const { createNewValue } = options;
    // Add new values that are not already present
    newMap?.forEach((valueInNewMap, key) => {
        if (!map.has(key)) {
            // New values
            map.set(key, createNewValue(key, valueInNewMap as U & K));
        }
    });
}

/** @internal */
export function isAbstractConstructorSymbol(symbol: Symbol): boolean {
    if (symbol.flags & SymbolFlags.Class) {
        const declaration = getClassLikeDeclarationOfSymbol(symbol);
        return !!declaration && hasSyntacticModifier(declaration, ModifierFlags.Abstract);
    }
    return false;
}

/** @internal */
export function getClassLikeDeclarationOfSymbol(symbol: Symbol): ClassLikeDeclaration | undefined {
    return symbol.declarations?.find(isClassLike);
}

/** @internal */
export function getObjectFlags(type: Type): ObjectFlags {
    return type.flags & TypeFlags.ObjectFlagsType ? (type as ObjectFlagsType).objectFlags : 0;
}

/** @internal */
export function forSomeAncestorDirectory(directory: string, callback: (directory: string) => boolean): boolean {
    return !!forEachAncestorDirectory(directory, d => callback(d) ? true : undefined);
}

/** @internal */
export function isUMDExportSymbol(symbol: Symbol | undefined): boolean {
    return !!symbol && !!symbol.declarations && !!symbol.declarations[0] && isNamespaceExportDeclaration(symbol.declarations[0]);
}

/** @internal */
export function showModuleSpecifier({ moduleSpecifier }: ImportDeclaration): string {
    return isStringLiteral(moduleSpecifier) ? moduleSpecifier.text : getTextOfNode(moduleSpecifier);
}

/** @internal */
export function getLastChild(node: Node): Node | undefined {
    let lastChild: Node | undefined;
    forEachChild(node, child => {
        if (nodeIsPresent(child)) lastChild = child;
    }, children => {
        // As an optimization, jump straight to the end of the list.
        for (let i = children.length - 1; i >= 0; i--) {
            if (nodeIsPresent(children[i])) {
                lastChild = children[i];
                break;
            }
        }
    });
    return lastChild;
}

/**
 * Add a value to a set, and return true if it wasn't already present.
 *
 * @internal
 */
export function addToSeen<K>(seen: Map<K, true>, key: K): boolean;
/** @internal */
export function addToSeen<K, T>(seen: Map<K, T>, key: K, value: T): boolean;
/** @internal */
export function addToSeen<K, T>(seen: Map<K, T>, key: K, value: T = true as any): boolean {
    if (seen.has(key)) {
        return false;
    }
    seen.set(key, value);
    return true;
}

/** @internal */
export function isObjectTypeDeclaration(node: Node): node is ObjectTypeDeclaration {
    return isClassLike(node) || isInterfaceDeclaration(node) || isTypeLiteralNode(node);
}

/** @internal */
export function isTypeNodeKind(kind: SyntaxKind): kind is TypeNodeSyntaxKind {
    return (kind >= SyntaxKind.FirstTypeNode && kind <= SyntaxKind.LastTypeNode)
        || kind === SyntaxKind.AnyKeyword
        || kind === SyntaxKind.UnknownKeyword
        || kind === SyntaxKind.NumberKeyword
        || kind === SyntaxKind.BigIntKeyword
        || kind === SyntaxKind.ObjectKeyword
        || kind === SyntaxKind.BooleanKeyword
        || kind === SyntaxKind.StringKeyword
        || kind === SyntaxKind.SymbolKeyword
        || kind === SyntaxKind.VoidKeyword
        || kind === SyntaxKind.UndefinedKeyword
        || kind === SyntaxKind.NeverKeyword
        || kind === SyntaxKind.IntrinsicKeyword
        || kind === SyntaxKind.ExpressionWithTypeArguments
        || kind === SyntaxKind.JSDocAllType
        || kind === SyntaxKind.JSDocUnknownType
        || kind === SyntaxKind.JSDocNullableType
        || kind === SyntaxKind.JSDocNonNullableType
        || kind === SyntaxKind.JSDocOptionalType
        || kind === SyntaxKind.JSDocFunctionType
        || kind === SyntaxKind.JSDocVariadicType;
}

/** @internal */
export function isAccessExpression(node: Node): node is AccessExpression {
    return node.kind === SyntaxKind.PropertyAccessExpression || node.kind === SyntaxKind.ElementAccessExpression;
}

/** @internal */
export function getNameOfAccessExpression(node: AccessExpression) {
    if (node.kind === SyntaxKind.PropertyAccessExpression) {
        return node.name;
    }
    Debug.assert(node.kind === SyntaxKind.ElementAccessExpression);
    return node.argumentExpression;
}

/** @deprecated @internal */
export function isBundleFileTextLike(section: BundleFileSection): section is BundleFileTextLike {
    switch (section.kind) {
        case BundleFileSectionKind.Text:
        case BundleFileSectionKind.Internal:
            return true;
        default:
            return false;
    }
}

/** @internal */
export function isNamedImportsOrExports(node: Node): node is NamedImportsOrExports {
    return node.kind === SyntaxKind.NamedImports || node.kind === SyntaxKind.NamedExports;
}

/** @internal */
export function getLeftmostAccessExpression(expr: Expression): Expression {
    while (isAccessExpression(expr)) {
        expr = expr.expression;
    }
    return expr;
}

/** @internal */
export function forEachNameInAccessChainWalkingLeft<T>(name: MemberName | StringLiteralLike, action: (name: MemberName | StringLiteralLike) => T | undefined): T | undefined {
    if (isAccessExpression(name.parent) && isRightSideOfAccessExpression(name)) {
        return walkAccessExpression(name.parent);
    }

    function walkAccessExpression(access: AccessExpression): T | undefined {
        if (access.kind === SyntaxKind.PropertyAccessExpression) {
            const res = action(access.name);
            if (res !== undefined) {
                return res;
            }
        }
        else if (access.kind === SyntaxKind.ElementAccessExpression) {
            if (isIdentifier(access.argumentExpression) || isStringLiteralLike(access.argumentExpression)) {
                const res = action(access.argumentExpression);
                if (res !== undefined) {
                    return res;
                }
            }
            else {
                // Chain interrupted by non-static-name access 'x[expr()].y.z'
                return undefined;
            }
        }

        if (isAccessExpression(access.expression)) {
            return walkAccessExpression(access.expression);
        }
        if (isIdentifier(access.expression)) {
            // End of chain at Identifier 'x.y.z'
            return action(access.expression);
        }
        // End of chain at non-Identifier 'x().y.z'
        return undefined;
    }
}

/** @internal */
export function getLeftmostExpression(node: Expression, stopAtCallExpressions: boolean) {
    while (true) {
        switch (node.kind) {
            case SyntaxKind.PostfixUnaryExpression:
                node = (node as PostfixUnaryExpression).operand;
                continue;

            case SyntaxKind.BinaryExpression:
                node = (node as BinaryExpression).left;
                continue;

            case SyntaxKind.ConditionalExpression:
                node = (node as ConditionalExpression).condition;
                continue;

            case SyntaxKind.TaggedTemplateExpression:
                node = (node as TaggedTemplateExpression).tag;
                continue;

            case SyntaxKind.CallExpression:
                if (stopAtCallExpressions) {
                    return node;
                }
                // falls through
            case SyntaxKind.AsExpression:
            case SyntaxKind.ElementAccessExpression:
            case SyntaxKind.PropertyAccessExpression:
            case SyntaxKind.NonNullExpression:
            case SyntaxKind.PartiallyEmittedExpression:
            case SyntaxKind.SatisfiesExpression:
                node = (node as CallExpression | PropertyAccessExpression | ElementAccessExpression | AsExpression | NonNullExpression | PartiallyEmittedExpression | SatisfiesExpression).expression;
                continue;
        }

        return node;
    }
}

/** @internal */
export interface ObjectAllocator {
    getNodeConstructor(): new (kind: SyntaxKind, pos: number, end: number) => Node;
    getTokenConstructor(): new <TKind extends SyntaxKind>(kind: TKind, pos: number, end: number) => Token<TKind>;
    getIdentifierConstructor(): new (kind: SyntaxKind.Identifier, pos: number, end: number) => Identifier;
    getPrivateIdentifierConstructor(): new (kind: SyntaxKind.PrivateIdentifier, pos: number, end: number) => PrivateIdentifier;
    getSourceFileConstructor(): new (kind: SyntaxKind.SourceFile, pos: number, end: number) => SourceFile;
    getSymbolConstructor(): new (flags: SymbolFlags, name: __String) => Symbol;
    getTypeConstructor(): new (checker: TypeChecker, flags: TypeFlags) => Type;
    getSignatureConstructor(): new (checker: TypeChecker, flags: SignatureFlags) => Signature;
    getSourceMapSourceConstructor(): new (fileName: string, text: string, skipTrivia?: (pos: number) => number) => SourceMapSource;
}

function Symbol(this: Symbol, flags: SymbolFlags, name: __String) {
    this.flags = flags;
    this.escapedName = name;
    this.declarations = undefined;
    this.valueDeclaration = undefined;
    this.id = 0;
    this.mergeId = 0;
    this.parent = undefined;
    this.members = undefined;
    this.exports = undefined;
    this.exportSymbol = undefined;
    this.constEnumOnlyModule = undefined;
    this.isReferenced = undefined;
    this.lastAssignmentPos = undefined;
    (this as any).links = undefined; // used by TransientSymbol
}

function Type(this: Type, checker: TypeChecker, flags: TypeFlags) {
    this.flags = flags;
    if (Debug.isDebugging || tracing) {
        this.checker = checker;
    }
}

function Signature(this: Signature, checker: TypeChecker, flags: SignatureFlags) {
    this.flags = flags;
    if (Debug.isDebugging) {
        this.checker = checker;
    }
}

function Node(this: Mutable<Node>, kind: SyntaxKind, pos: number, end: number) {
    this.pos = pos;
    this.end = end;
    this.kind = kind;
    this.id = 0;
    this.flags = NodeFlags.None;
    this.modifierFlagsCache = ModifierFlags.None;
    this.transformFlags = TransformFlags.None;
    this.parent = undefined!;
    this.original = undefined;
    this.emitNode = undefined;
}

function Token(this: Mutable<Node>, kind: SyntaxKind, pos: number, end: number) {
    this.pos = pos;
    this.end = end;
    this.kind = kind;
    this.id = 0;
    this.flags = NodeFlags.None;
    this.transformFlags = TransformFlags.None;
    this.parent = undefined!;
    this.emitNode = undefined;
}

function Identifier(this: Mutable<Node>, kind: SyntaxKind, pos: number, end: number) {
    this.pos = pos;
    this.end = end;
    this.kind = kind;
    this.id = 0;
    this.flags = NodeFlags.None;
    this.transformFlags = TransformFlags.None;
    this.parent = undefined!;
    this.original = undefined;
    this.emitNode = undefined;
}

function SourceMapSource(this: SourceMapSource, fileName: string, text: string, skipTrivia?: (pos: number) => number) {
    this.fileName = fileName;
    this.text = text;
    this.skipTrivia = skipTrivia || (pos => pos);
}

/** @internal */
export const objectAllocator: ObjectAllocator = {
    getNodeConstructor: () => Node as any,
    getTokenConstructor: () => Token as any,
    getIdentifierConstructor: () => Identifier as any,
    getPrivateIdentifierConstructor: () => Node as any,
    getSourceFileConstructor: () => Node as any,
    getSymbolConstructor: () => Symbol as any,
    getTypeConstructor: () => Type as any,
    getSignatureConstructor: () => Signature as any,
    getSourceMapSourceConstructor: () => SourceMapSource as any,
};

const objectAllocatorPatchers: ((objectAllocator: ObjectAllocator) => void)[] = [];

/**
 * Used by `deprecatedCompat` to patch the object allocator to apply deprecations.
 * @internal
 */
export function addObjectAllocatorPatcher(fn: (objectAllocator: ObjectAllocator) => void) {
    objectAllocatorPatchers.push(fn);
    fn(objectAllocator);
}

/** @internal */
export function setObjectAllocator(alloc: ObjectAllocator) {
    Object.assign(objectAllocator, alloc);
    forEach(objectAllocatorPatchers, fn => fn(objectAllocator));
}

/** @internal */
export function formatStringFromArgs(text: string, args: DiagnosticArguments): string {
    return text.replace(/{(\d+)}/g, (_match, index: string) => "" + Debug.checkDefined(args[+index]));
}

let localizedDiagnosticMessages: MapLike<string> | undefined;

/** @internal */
export function setLocalizedDiagnosticMessages(messages: MapLike<string> | undefined) {
    localizedDiagnosticMessages = messages;
}

/** @internal */
// If the localized messages json is unset, and if given function use it to set the json

export function maybeSetLocalizedDiagnosticMessages(getMessages: undefined | (() => MapLike<string> | undefined)) {
    if (!localizedDiagnosticMessages && getMessages) {
        localizedDiagnosticMessages = getMessages();
    }
}

/** @internal */
export function getLocaleSpecificMessage(message: DiagnosticMessage) {
    return localizedDiagnosticMessages && localizedDiagnosticMessages[message.key] || message.message;
}

/** @internal */
export function createDetachedDiagnostic(fileName: string, sourceText: string, start: number, length: number, message: DiagnosticMessage, ...args: DiagnosticArguments): DiagnosticWithDetachedLocation {
    if ((start + length) > sourceText.length) {
        length = sourceText.length - start;
    }

    assertDiagnosticLocation(sourceText, start, length);
    let text = getLocaleSpecificMessage(message);

    if (some(args)) {
        text = formatStringFromArgs(text, args);
    }

    return {
        file: undefined,
        start,
        length,

        messageText: text,
        category: message.category,
        code: message.code,
        reportsUnnecessary: message.reportsUnnecessary,
        fileName,
    };
}

function isDiagnosticWithDetachedLocation(diagnostic: DiagnosticRelatedInformation | DiagnosticWithDetachedLocation): diagnostic is DiagnosticWithDetachedLocation {
    return diagnostic.file === undefined
        && diagnostic.start !== undefined
        && diagnostic.length !== undefined
        && typeof (diagnostic as DiagnosticWithDetachedLocation).fileName === "string";
}

function attachFileToDiagnostic(diagnostic: DiagnosticWithDetachedLocation, file: SourceFile): DiagnosticWithLocation {
    const fileName = file.fileName || "";
    const length = file.text.length;
    Debug.assertEqual(diagnostic.fileName, fileName);
    Debug.assertLessThanOrEqual(diagnostic.start, length);
    Debug.assertLessThanOrEqual(diagnostic.start + diagnostic.length, length);
    const diagnosticWithLocation: DiagnosticWithLocation = {
        file,
        start: diagnostic.start,
        length: diagnostic.length,
        messageText: diagnostic.messageText,
        category: diagnostic.category,
        code: diagnostic.code,
        reportsUnnecessary: diagnostic.reportsUnnecessary,
    };
    if (diagnostic.relatedInformation) {
        diagnosticWithLocation.relatedInformation = [];
        for (const related of diagnostic.relatedInformation) {
            if (isDiagnosticWithDetachedLocation(related) && related.fileName === fileName) {
                Debug.assertLessThanOrEqual(related.start, length);
                Debug.assertLessThanOrEqual(related.start + related.length, length);
                diagnosticWithLocation.relatedInformation.push(attachFileToDiagnostic(related, file));
            }
            else {
                diagnosticWithLocation.relatedInformation.push(related);
            }
        }
    }
    return diagnosticWithLocation;
}

/** @internal */
export function attachFileToDiagnostics(diagnostics: DiagnosticWithDetachedLocation[], file: SourceFile): DiagnosticWithLocation[] {
    const diagnosticsWithLocation: DiagnosticWithLocation[] = [];
    for (const diagnostic of diagnostics) {
        diagnosticsWithLocation.push(attachFileToDiagnostic(diagnostic, file));
    }
    return diagnosticsWithLocation;
}

/** @internal */
export function createFileDiagnostic(file: SourceFile, start: number, length: number, message: DiagnosticMessage, ...args: DiagnosticArguments): DiagnosticWithLocation {
    assertDiagnosticLocation(file.text, start, length);

    let text = getLocaleSpecificMessage(message);

    if (some(args)) {
        text = formatStringFromArgs(text, args);
    }

    return {
        file,
        start,
        length,

        messageText: text,
        category: message.category,
        code: message.code,
        reportsUnnecessary: message.reportsUnnecessary,
        reportsDeprecated: message.reportsDeprecated,
    };
}

/** @internal */
export function formatMessage(message: DiagnosticMessage, ...args: DiagnosticArguments): string {
    let text = getLocaleSpecificMessage(message);

    if (some(args)) {
        text = formatStringFromArgs(text, args);
    }

    return text;
}

/** @internal */
export function createCompilerDiagnostic(message: DiagnosticMessage, ...args: DiagnosticArguments): Diagnostic {
    let text = getLocaleSpecificMessage(message);

    if (some(args)) {
        text = formatStringFromArgs(text, args);
    }

    return {
        file: undefined,
        start: undefined,
        length: undefined,

        messageText: text,
        category: message.category,
        code: message.code,
        reportsUnnecessary: message.reportsUnnecessary,
        reportsDeprecated: message.reportsDeprecated,
    };
}

/** @internal */
export function createCompilerDiagnosticFromMessageChain(chain: DiagnosticMessageChain, relatedInformation?: DiagnosticRelatedInformation[]): Diagnostic {
    return {
        file: undefined,
        start: undefined,
        length: undefined,

        code: chain.code,
        category: chain.category,
        messageText: chain.next ? chain : chain.messageText,
        relatedInformation,
    };
}

/** @internal */
export function chainDiagnosticMessages(details: DiagnosticMessageChain | DiagnosticMessageChain[] | undefined, message: DiagnosticMessage, ...args: DiagnosticArguments): DiagnosticMessageChain {
    let text = getLocaleSpecificMessage(message);

    if (some(args)) {
        text = formatStringFromArgs(text, args);
    }
    return {
        messageText: text,
        category: message.category,
        code: message.code,

        next: details === undefined || Array.isArray(details) ? details : [details],
    };
}

/** @internal */
export function concatenateDiagnosticMessageChains(headChain: DiagnosticMessageChain, tailChain: DiagnosticMessageChain): void {
    let lastChain = headChain;
    while (lastChain.next) {
        lastChain = lastChain.next[0];
    }

    lastChain.next = [tailChain];
}

function getDiagnosticFilePath(diagnostic: Diagnostic): string | undefined {
    return diagnostic.file ? diagnostic.file.path : undefined;
}

/** @internal */
export function compareDiagnostics(d1: Diagnostic, d2: Diagnostic): Comparison {
    return compareDiagnosticsSkipRelatedInformation(d1, d2) ||
        compareRelatedInformation(d1, d2) ||
        Comparison.EqualTo;
}

/** @internal */
export function compareDiagnosticsSkipRelatedInformation(d1: Diagnostic, d2: Diagnostic): Comparison {
    return compareStringsCaseSensitive(getDiagnosticFilePath(d1), getDiagnosticFilePath(d2)) ||
        compareValues(d1.start, d2.start) ||
        compareValues(d1.length, d2.length) ||
        compareValues(d1.code, d2.code) ||
        compareMessageText(d1.messageText, d2.messageText) ||
        Comparison.EqualTo;
}

function compareRelatedInformation(d1: Diagnostic, d2: Diagnostic): Comparison {
    if (!d1.relatedInformation && !d2.relatedInformation) {
        return Comparison.EqualTo;
    }
    if (d1.relatedInformation && d2.relatedInformation) {
        return compareValues(d1.relatedInformation.length, d2.relatedInformation.length) || forEach(d1.relatedInformation, (d1i, index) => {
            const d2i = d2.relatedInformation![index];
            return compareDiagnostics(d1i, d2i); // EqualTo is 0, so falsy, and will cause the next item to be compared
        }) || Comparison.EqualTo;
    }
    return d1.relatedInformation ? Comparison.LessThan : Comparison.GreaterThan;
}

function compareMessageText(t1: string | DiagnosticMessageChain, t2: string | DiagnosticMessageChain): Comparison {
    if (typeof t1 === "string" && typeof t2 === "string") {
        return compareStringsCaseSensitive(t1, t2);
    }
    else if (typeof t1 === "string") {
        return Comparison.LessThan;
    }
    else if (typeof t2 === "string") {
        return Comparison.GreaterThan;
    }
    let res = compareStringsCaseSensitive(t1.messageText, t2.messageText);
    if (res) {
        return res;
    }
    if (!t1.next && !t2.next) {
        return Comparison.EqualTo;
    }
    if (!t1.next) {
        return Comparison.LessThan;
    }
    if (!t2.next) {
        return Comparison.GreaterThan;
    }
    const len = Math.min(t1.next.length, t2.next.length);
    for (let i = 0; i < len; i++) {
        res = compareMessageText(t1.next[i], t2.next[i]);
        if (res) {
            return res;
        }
    }
    if (t1.next.length < t2.next.length) {
        return Comparison.LessThan;
    }
    else if (t1.next.length > t2.next.length) {
        return Comparison.GreaterThan;
    }
    return Comparison.EqualTo;
}

/** @internal */
export function getLanguageVariant(scriptKind: ScriptKind) {
    // .tsx and .jsx files are treated as jsx language variant.
    return scriptKind === ScriptKind.TSX || scriptKind === ScriptKind.JSX || scriptKind === ScriptKind.JS || scriptKind === ScriptKind.JSON ? LanguageVariant.JSX : LanguageVariant.Standard;
}

/**
 * This is a somewhat unavoidable full tree walk to locate a JSX tag - `import.meta` requires the same,
 * but we avoid that walk (or parts of it) if at all possible using the `PossiblyContainsImportMeta` node flag.
 * Unfortunately, there's no `NodeFlag` space to do the same for JSX.
 */
function walkTreeForJSXTags(node: Node): Node | undefined {
    if (!(node.transformFlags & TransformFlags.ContainsJsx)) return undefined;
    return isJsxOpeningLikeElement(node) || isJsxFragment(node) ? node : forEachChild(node, walkTreeForJSXTags);
}

function isFileModuleFromUsingJSXTag(file: SourceFile): Node | undefined {
    // Excludes declaration files - they still require an explicit `export {}` or the like
    // for back compat purposes. (not that declaration files should contain JSX tags!)
    return !file.isDeclarationFile ? walkTreeForJSXTags(file) : undefined;
}

/**
 * Note that this requires file.impliedNodeFormat be set already; meaning it must be set very early on
 * in SourceFile construction.
 */
function isFileForcedToBeModuleByFormat(file: SourceFile): true | undefined {
    // Excludes declaration files - they still require an explicit `export {}` or the like
    // for back compat purposes. The only non-declaration files _not_ forced to be a module are `.js` files
    // that aren't esm-mode (meaning not in a `type: module` scope).
    return (file.impliedNodeFormat === ModuleKind.ESNext || (fileExtensionIsOneOf(file.fileName, [Extension.Cjs, Extension.Cts, Extension.Mjs, Extension.Mts]))) && !file.isDeclarationFile ? true : undefined;
}

/** @internal */
export function getSetExternalModuleIndicator(options: CompilerOptions): (file: SourceFile) => void {
    // TODO: Should this callback be cached?
    switch (getEmitModuleDetectionKind(options)) {
        case ModuleDetectionKind.Force:
            // All non-declaration files are modules, declaration files still do the usual isFileProbablyExternalModule
            return (file: SourceFile) => {
                file.externalModuleIndicator = isFileProbablyExternalModule(file) || !file.isDeclarationFile || undefined;
            };
        case ModuleDetectionKind.Legacy:
            // Files are modules if they have imports, exports, or import.meta
            return (file: SourceFile) => {
                file.externalModuleIndicator = isFileProbablyExternalModule(file);
            };
        case ModuleDetectionKind.Auto:
            // If module is nodenext or node16, all esm format files are modules
            // If jsx is react-jsx or react-jsxdev then jsx tags force module-ness
            // otherwise, the presence of import or export statments (or import.meta) implies module-ness
            const checks: ((file: SourceFile) => Node | true | undefined)[] = [isFileProbablyExternalModule];
            if (options.jsx === JsxEmit.ReactJSX || options.jsx === JsxEmit.ReactJSXDev) {
                checks.push(isFileModuleFromUsingJSXTag);
            }
            checks.push(isFileForcedToBeModuleByFormat);
            const combined = or(...checks);
            const callback = (file: SourceFile) => void (file.externalModuleIndicator = combined(file));
            return callback;
    }
}

type CompilerOptionKeys = keyof { [K in keyof CompilerOptions as string extends K ? never : K]: any; };
function createComputedCompilerOptions<T extends Record<string, CompilerOptionKeys[]>>(
    options: {
        [K in keyof T & CompilerOptionKeys | StrictOptionName]: {
            dependencies: T[K];
            computeValue: (compilerOptions: Pick<CompilerOptions, K | T[K][number]>) => Exclude<CompilerOptions[K], undefined>;
        };
    },
) {
    return options;
}

/** @internal */
export const computedOptions = createComputedCompilerOptions({
    target: {
        dependencies: ["module"],
        computeValue: compilerOptions => {
            return compilerOptions.target ??
                ((compilerOptions.module === ModuleKind.Node16 && ScriptTarget.ES2022) ||
                    (compilerOptions.module === ModuleKind.NodeNext && ScriptTarget.ESNext) ||
                    ScriptTarget.ES5);
        },
    },
    module: {
        dependencies: ["target"],
        computeValue: (compilerOptions): ModuleKind => {
            return typeof compilerOptions.module === "number" ?
                compilerOptions.module :
                computedOptions.target.computeValue(compilerOptions) >= ScriptTarget.ES2015 ? ModuleKind.ES2015 : ModuleKind.CommonJS;
        },
    },
    moduleResolution: {
        dependencies: ["module", "target"],
        computeValue: (compilerOptions): ModuleResolutionKind => {
            let moduleResolution = compilerOptions.moduleResolution;
            if (moduleResolution === undefined) {
                switch (computedOptions.module.computeValue(compilerOptions)) {
                    case ModuleKind.CommonJS:
                        moduleResolution = ModuleResolutionKind.Node10;
                        break;
                    case ModuleKind.Node16:
                        moduleResolution = ModuleResolutionKind.Node16;
                        break;
                    case ModuleKind.NodeNext:
                        moduleResolution = ModuleResolutionKind.NodeNext;
                        break;
                    case ModuleKind.Preserve:
                        moduleResolution = ModuleResolutionKind.Bundler;
                        break;
                    default:
                        moduleResolution = ModuleResolutionKind.Classic;
                        break;
                }
            }
            return moduleResolution;
        },
    },
    moduleDetection: {
        dependencies: ["module", "target"],
        computeValue: (compilerOptions): ModuleDetectionKind => {
            return compilerOptions.moduleDetection ||
                (computedOptions.module.computeValue(compilerOptions) === ModuleKind.Node16 ||
                        computedOptions.module.computeValue(compilerOptions) === ModuleKind.NodeNext ? ModuleDetectionKind.Force : ModuleDetectionKind.Auto);
        },
    },
    isolatedModules: {
        dependencies: ["verbatimModuleSyntax"],
        computeValue: compilerOptions => {
            return !!(compilerOptions.isolatedModules || compilerOptions.verbatimModuleSyntax);
        },
    },
    esModuleInterop: {
        dependencies: ["module", "target"],
        computeValue: (compilerOptions): boolean => {
            if (compilerOptions.esModuleInterop !== undefined) {
                return compilerOptions.esModuleInterop;
            }
            switch (computedOptions.module.computeValue(compilerOptions)) {
                case ModuleKind.Node16:
                case ModuleKind.NodeNext:
                case ModuleKind.Preserve:
                    return true;
            }
            return false;
        },
    },
    allowSyntheticDefaultImports: {
        dependencies: ["module", "target", "moduleResolution"],
        computeValue: (compilerOptions): boolean => {
            if (compilerOptions.allowSyntheticDefaultImports !== undefined) {
                return compilerOptions.allowSyntheticDefaultImports;
            }
            return computedOptions.esModuleInterop.computeValue(compilerOptions)
                || computedOptions.module.computeValue(compilerOptions) === ModuleKind.System
                || computedOptions.moduleResolution.computeValue(compilerOptions) === ModuleResolutionKind.Bundler;
        },
    },
    resolvePackageJsonExports: {
        dependencies: ["moduleResolution"],
        computeValue: (compilerOptions): boolean => {
            const moduleResolution = computedOptions.moduleResolution.computeValue(compilerOptions);
            if (!moduleResolutionSupportsPackageJsonExportsAndImports(moduleResolution)) {
                return false;
            }
            if (compilerOptions.resolvePackageJsonExports !== undefined) {
                return compilerOptions.resolvePackageJsonExports;
            }
            switch (moduleResolution) {
                case ModuleResolutionKind.Node16:
                case ModuleResolutionKind.NodeNext:
                case ModuleResolutionKind.Bundler:
                    return true;
            }
            return false;
        },
    },
    resolvePackageJsonImports: {
        dependencies: ["moduleResolution", "resolvePackageJsonExports"],
        computeValue: (compilerOptions): boolean => {
            const moduleResolution = computedOptions.moduleResolution.computeValue(compilerOptions);
            if (!moduleResolutionSupportsPackageJsonExportsAndImports(moduleResolution)) {
                return false;
            }
            if (compilerOptions.resolvePackageJsonExports !== undefined) {
                return compilerOptions.resolvePackageJsonExports;
            }
            switch (moduleResolution) {
                case ModuleResolutionKind.Node16:
                case ModuleResolutionKind.NodeNext:
                case ModuleResolutionKind.Bundler:
                    return true;
            }
            return false;
        },
    },
    resolveJsonModule: {
        dependencies: ["moduleResolution", "module", "target"],
        computeValue: (compilerOptions): boolean => {
            if (compilerOptions.resolveJsonModule !== undefined) {
                return compilerOptions.resolveJsonModule;
            }
            return computedOptions.moduleResolution.computeValue(compilerOptions) === ModuleResolutionKind.Bundler;
        },
    },
    declaration: {
        dependencies: ["composite"],
        computeValue: compilerOptions => {
            return !!(compilerOptions.declaration || compilerOptions.composite);
        },
    },
    preserveConstEnums: {
        dependencies: ["isolatedModules", "verbatimModuleSyntax"],
        computeValue: (compilerOptions): boolean => {
            return !!(compilerOptions.preserveConstEnums || computedOptions.isolatedModules.computeValue(compilerOptions));
        },
    },
    incremental: {
        dependencies: ["composite"],
        computeValue: compilerOptions => {
            return !!(compilerOptions.incremental || compilerOptions.composite);
        },
    },
    declarationMap: {
        dependencies: ["declaration", "composite"],
        computeValue: (compilerOptions): boolean => {
            return !!(compilerOptions.declarationMap && computedOptions.declaration.computeValue(compilerOptions));
        },
    },
    allowJs: {
        dependencies: ["checkJs"],
        computeValue: compilerOptions => {
            return compilerOptions.allowJs === undefined ? !!compilerOptions.checkJs : compilerOptions.allowJs;
        },
    },
    useDefineForClassFields: {
        dependencies: ["target", "module"],
        computeValue: (compilerOptions): boolean => {
            return compilerOptions.useDefineForClassFields === undefined
                ? computedOptions.target.computeValue(compilerOptions) >= ScriptTarget.ES2022
                : compilerOptions.useDefineForClassFields;
        },
    },
    noImplicitAny: {
        dependencies: ["strict"],
        computeValue: compilerOptions => {
            return getStrictOptionValue(compilerOptions, "noImplicitAny");
        },
    },
    noImplicitThis: {
        dependencies: ["strict"],
        computeValue: compilerOptions => {
            return getStrictOptionValue(compilerOptions, "noImplicitThis");
        },
    },
    strictNullChecks: {
        dependencies: ["strict"],
        computeValue: compilerOptions => {
            return getStrictOptionValue(compilerOptions, "strictNullChecks");
        },
    },
    strictFunctionTypes: {
        dependencies: ["strict"],
        computeValue: compilerOptions => {
            return getStrictOptionValue(compilerOptions, "strictFunctionTypes");
        },
    },
    strictBindCallApply: {
        dependencies: ["strict"],
        computeValue: compilerOptions => {
            return getStrictOptionValue(compilerOptions, "strictBindCallApply");
        },
    },
    strictPropertyInitialization: {
        dependencies: ["strict"],
        computeValue: compilerOptions => {
            return getStrictOptionValue(compilerOptions, "strictPropertyInitialization");
        },
    },
    alwaysStrict: {
        dependencies: ["strict"],
        computeValue: compilerOptions => {
            return getStrictOptionValue(compilerOptions, "alwaysStrict");
        },
    },
    useUnknownInCatchVariables: {
        dependencies: ["strict"],
        computeValue: compilerOptions => {
            return getStrictOptionValue(compilerOptions, "useUnknownInCatchVariables");
        },
    },
});

/** @internal */
export const getEmitScriptTarget = computedOptions.target.computeValue;
/** @internal */
export const getEmitModuleKind = computedOptions.module.computeValue;
/** @internal */
export const getEmitModuleResolutionKind = computedOptions.moduleResolution.computeValue;
/** @internal */
export const getEmitModuleDetectionKind = computedOptions.moduleDetection.computeValue;
/** @internal */
export const getIsolatedModules = computedOptions.isolatedModules.computeValue;
/** @internal */
export const getESModuleInterop = computedOptions.esModuleInterop.computeValue;
/** @internal */
export const getAllowSyntheticDefaultImports = computedOptions.allowSyntheticDefaultImports.computeValue;
/** @internal */
export const getResolvePackageJsonExports = computedOptions.resolvePackageJsonExports.computeValue;
/** @internal */
export const getResolvePackageJsonImports = computedOptions.resolvePackageJsonImports.computeValue;
/** @internal */
export const getResolveJsonModule = computedOptions.resolveJsonModule.computeValue;
/** @internal */
export const getEmitDeclarations = computedOptions.declaration.computeValue;
/** @internal */
export const shouldPreserveConstEnums = computedOptions.preserveConstEnums.computeValue;
/** @internal */
export const isIncrementalCompilation = computedOptions.incremental.computeValue;
/** @internal */
export const getAreDeclarationMapsEnabled = computedOptions.declarationMap.computeValue;
/** @internal */
export const getAllowJSCompilerOption = computedOptions.allowJs.computeValue;
/** @internal */
export const getUseDefineForClassFields = computedOptions.useDefineForClassFields.computeValue;

/** @internal */
export function emitModuleKindIsNonNodeESM(moduleKind: ModuleKind) {
    return moduleKind >= ModuleKind.ES2015 && moduleKind <= ModuleKind.ESNext;
}

/** @internal */
export function hasJsonModuleEmitEnabled(options: CompilerOptions) {
    switch (getEmitModuleKind(options)) {
        case ModuleKind.None:
        case ModuleKind.System:
        case ModuleKind.UMD:
            return false;
    }
    return true;
}

/** @internal */
export function importNameElisionDisabled(options: CompilerOptions) {
    return options.verbatimModuleSyntax || options.isolatedModules && options.preserveValueImports;
}

/** @internal */
export function unreachableCodeIsError(options: CompilerOptions): boolean {
    return options.allowUnreachableCode === false;
}

/** @internal */
export function unusedLabelIsError(options: CompilerOptions): boolean {
    return options.allowUnusedLabels === false;
}

/** @internal */
export function moduleResolutionSupportsPackageJsonExportsAndImports(moduleResolution: ModuleResolutionKind): boolean {
    return moduleResolution >= ModuleResolutionKind.Node16 && moduleResolution <= ModuleResolutionKind.NodeNext
        || moduleResolution === ModuleResolutionKind.Bundler;
}

/** @internal */
export type StrictOptionName =
    | "noImplicitAny"
    | "noImplicitThis"
    | "strictNullChecks"
    | "strictFunctionTypes"
    | "strictBindCallApply"
    | "strictPropertyInitialization"
    | "alwaysStrict"
    | "useUnknownInCatchVariables";

/** @internal */
export function getStrictOptionValue(compilerOptions: CompilerOptions, flag: StrictOptionName): boolean {
    return compilerOptions[flag] === undefined ? !!compilerOptions.strict : !!compilerOptions[flag];
}

/** @internal */
export function getEmitStandardClassFields(compilerOptions: CompilerOptions) {
    return compilerOptions.useDefineForClassFields !== false && getEmitScriptTarget(compilerOptions) >= ScriptTarget.ES2022;
}

/** @internal */
export function compilerOptionsAffectSemanticDiagnostics(newOptions: CompilerOptions, oldOptions: CompilerOptions): boolean {
    return optionsHaveChanges(oldOptions, newOptions, semanticDiagnosticsOptionDeclarations);
}

/** @internal */
export function compilerOptionsAffectEmit(newOptions: CompilerOptions, oldOptions: CompilerOptions): boolean {
    return optionsHaveChanges(oldOptions, newOptions, affectsEmitOptionDeclarations);
}

/** @internal */
export function compilerOptionsAffectDeclarationPath(newOptions: CompilerOptions, oldOptions: CompilerOptions): boolean {
    return optionsHaveChanges(oldOptions, newOptions, affectsDeclarationPathOptionDeclarations);
}

/** @internal */
export function getCompilerOptionValue(options: CompilerOptions, option: CommandLineOption): unknown {
    return option.strictFlag ? getStrictOptionValue(options, option.name as StrictOptionName) :
        option.allowJsFlag ? getAllowJSCompilerOption(options) :
        options[option.name];
}

/** @internal */
export function getJSXTransformEnabled(options: CompilerOptions): boolean {
    const jsx = options.jsx;
    return jsx === JsxEmit.React || jsx === JsxEmit.ReactJSX || jsx === JsxEmit.ReactJSXDev;
}

/** @internal */
export function getJSXImplicitImportBase(compilerOptions: CompilerOptions, file?: SourceFile): string | undefined {
    const jsxImportSourcePragmas = file?.pragmas.get("jsximportsource");
    const jsxImportSourcePragma = isArray(jsxImportSourcePragmas) ? jsxImportSourcePragmas[jsxImportSourcePragmas.length - 1] : jsxImportSourcePragmas;
    return compilerOptions.jsx === JsxEmit.ReactJSX ||
            compilerOptions.jsx === JsxEmit.ReactJSXDev ||
            compilerOptions.jsxImportSource ||
            jsxImportSourcePragma ?
        jsxImportSourcePragma?.arguments.factory || compilerOptions.jsxImportSource || "react" :
        undefined;
}

/** @internal */
export function getJSXRuntimeImport(base: string | undefined, options: CompilerOptions) {
    return base ? `${base}/${options.jsx === JsxEmit.ReactJSXDev ? "jsx-dev-runtime" : "jsx-runtime"}` : undefined;
}

/** @internal */
export function hasZeroOrOneAsteriskCharacter(str: string): boolean {
    let seenAsterisk = false;
    for (let i = 0; i < str.length; i++) {
        if (str.charCodeAt(i) === CharacterCodes.asterisk) {
            if (!seenAsterisk) {
                seenAsterisk = true;
            }
            else {
                // have already seen asterisk
                return false;
            }
        }
    }
    return true;
}

/** @internal */
export interface SymlinkedDirectory {
    /**
     * Matches the casing returned by `realpath`.  Used to compute the `realpath` of children.
     * Always has trailing directory separator
     */
    real: string;
    /**
     * toPath(real).  Stored to avoid repeated recomputation.
     * Always has trailing directory separator
     */
    realPath: Path;
}

/** @internal */
export interface SymlinkCache {
    /** Gets a map from symlink to realpath. Keys have trailing directory separators. */
    getSymlinkedDirectories(): ReadonlyMap<Path, SymlinkedDirectory | false> | undefined;
    /** Gets a map from realpath to symlinks. Keys have trailing directory separators. */
    getSymlinkedDirectoriesByRealpath(): MultiMap<Path, string> | undefined;
    /** Gets a map from symlink to realpath */
    getSymlinkedFiles(): ReadonlyMap<Path, string> | undefined;
    setSymlinkedDirectory(symlink: string, real: SymlinkedDirectory | false): void;
    setSymlinkedFile(symlinkPath: Path, real: string): void;
    /**
     * @internal
     * Uses resolvedTypeReferenceDirectives from program instead of from files, since files
     * don't include automatic type reference directives. Must be called only when
     * `hasProcessedResolutions` returns false (once per cache instance).
     */
    setSymlinksFromResolutions(
        forEachResolvedModule: (
            callback: (resolution: ResolvedModuleWithFailedLookupLocations, moduleName: string, mode: ResolutionMode, filePath: Path) => void,
        ) => void,
        forEachResolvedTypeReferenceDirective: (
            callback: (resolution: ResolvedTypeReferenceDirectiveWithFailedLookupLocations, moduleName: string, mode: ResolutionMode, filePath: Path) => void,
        ) => void,
        typeReferenceDirectives: ModeAwareCache<ResolvedTypeReferenceDirectiveWithFailedLookupLocations>,
    ): void;
    /**
     * @internal
     * Whether `setSymlinksFromResolutions` has already been called.
     */
    hasProcessedResolutions(): boolean;
}

/** @internal */
export function createSymlinkCache(cwd: string, getCanonicalFileName: GetCanonicalFileName): SymlinkCache {
    let symlinkedDirectories: Map<Path, SymlinkedDirectory | false> | undefined;
    let symlinkedDirectoriesByRealpath: MultiMap<Path, string> | undefined;
    let symlinkedFiles: Map<Path, string> | undefined;
    let hasProcessedResolutions = false;
    return {
        getSymlinkedFiles: () => symlinkedFiles,
        getSymlinkedDirectories: () => symlinkedDirectories,
        getSymlinkedDirectoriesByRealpath: () => symlinkedDirectoriesByRealpath,
        setSymlinkedFile: (path, real) => (symlinkedFiles || (symlinkedFiles = new Map())).set(path, real),
        setSymlinkedDirectory: (symlink, real) => {
            // Large, interconnected dependency graphs in pnpm will have a huge number of symlinks
            // where both the realpath and the symlink path are inside node_modules/.pnpm. Since
            // this path is never a candidate for a module specifier, we can ignore it entirely.
            let symlinkPath = toPath(symlink, cwd, getCanonicalFileName);
            if (!containsIgnoredPath(symlinkPath)) {
                symlinkPath = ensureTrailingDirectorySeparator(symlinkPath);
                if (real !== false && !symlinkedDirectories?.has(symlinkPath)) {
                    (symlinkedDirectoriesByRealpath ||= createMultiMap()).add(real.realPath, symlink);
                }
                (symlinkedDirectories || (symlinkedDirectories = new Map())).set(symlinkPath, real);
            }
        },
        setSymlinksFromResolutions(forEachResolvedModule, forEachResolvedTypeReferenceDirective, typeReferenceDirectives) {
            Debug.assert(!hasProcessedResolutions);
            hasProcessedResolutions = true;
            forEachResolvedModule(resolution => processResolution(this, resolution.resolvedModule));
            forEachResolvedTypeReferenceDirective(resolution => processResolution(this, resolution.resolvedTypeReferenceDirective));
            typeReferenceDirectives.forEach(resolution => processResolution(this, resolution.resolvedTypeReferenceDirective));
        },
        hasProcessedResolutions: () => hasProcessedResolutions,
    };

    function processResolution(cache: SymlinkCache, resolution: ResolvedModuleFull | ResolvedTypeReferenceDirective | undefined) {
        if (!resolution || !resolution.originalPath || !resolution.resolvedFileName) return;
        const { resolvedFileName, originalPath } = resolution;
        cache.setSymlinkedFile(toPath(originalPath, cwd, getCanonicalFileName), resolvedFileName);
        const [commonResolved, commonOriginal] = guessDirectorySymlink(resolvedFileName, originalPath, cwd, getCanonicalFileName) || emptyArray;
        if (commonResolved && commonOriginal) {
            cache.setSymlinkedDirectory(
                commonOriginal,
                {
                    real: ensureTrailingDirectorySeparator(commonResolved),
                    realPath: ensureTrailingDirectorySeparator(toPath(commonResolved, cwd, getCanonicalFileName)),
                },
            );
        }
    }
}

function guessDirectorySymlink(a: string, b: string, cwd: string, getCanonicalFileName: GetCanonicalFileName): [string, string] | undefined {
    const aParts = getPathComponents(getNormalizedAbsolutePath(a, cwd));
    const bParts = getPathComponents(getNormalizedAbsolutePath(b, cwd));
    let isDirectory = false;
    while (
        aParts.length >= 2 && bParts.length >= 2 &&
        !isNodeModulesOrScopedPackageDirectory(aParts[aParts.length - 2], getCanonicalFileName) &&
        !isNodeModulesOrScopedPackageDirectory(bParts[bParts.length - 2], getCanonicalFileName) &&
        getCanonicalFileName(aParts[aParts.length - 1]) === getCanonicalFileName(bParts[bParts.length - 1])
    ) {
        aParts.pop();
        bParts.pop();
        isDirectory = true;
    }
    return isDirectory ? [getPathFromPathComponents(aParts), getPathFromPathComponents(bParts)] : undefined;
}

// KLUDGE: Don't assume one 'node_modules' links to another. More likely a single directory inside the node_modules is the symlink.
// ALso, don't assume that an `@foo` directory is linked. More likely the contents of that are linked.
function isNodeModulesOrScopedPackageDirectory(s: string | undefined, getCanonicalFileName: GetCanonicalFileName): boolean {
    return s !== undefined && (getCanonicalFileName(s) === "node_modules" || startsWith(s, "@"));
}

function stripLeadingDirectorySeparator(s: string): string | undefined {
    return isAnyDirectorySeparator(s.charCodeAt(0)) ? s.slice(1) : undefined;
}

/** @internal */
export function tryRemoveDirectoryPrefix(path: string, dirPath: string, getCanonicalFileName: GetCanonicalFileName): string | undefined {
    const withoutPrefix = tryRemovePrefix(path, dirPath, getCanonicalFileName);
    return withoutPrefix === undefined ? undefined : stripLeadingDirectorySeparator(withoutPrefix);
}

// Reserved characters, forces escaping of any non-word (or digit), non-whitespace character.
// It may be inefficient (we could just match (/[-[\]{}()*+?.,\\^$|#\s]/g), but this is future
// proof.
const reservedCharacterPattern = /[^\w\s/]/g;

/** @internal */
export function regExpEscape(text: string) {
    return text.replace(reservedCharacterPattern, escapeRegExpCharacter);
}

function escapeRegExpCharacter(match: string) {
    return "\\" + match;
}

const wildcardCharCodes = [CharacterCodes.asterisk, CharacterCodes.question];

/** @internal */
export const commonPackageFolders: readonly string[] = ["node_modules", "bower_components", "jspm_packages"];

const implicitExcludePathRegexPattern = `(?!(${commonPackageFolders.join("|")})(/|$))`;

/** @internal */
export interface WildcardMatcher {
    singleAsteriskRegexFragment: string;
    doubleAsteriskRegexFragment: string;
    replaceWildcardCharacter: (match: string) => string;
}

const filesMatcher: WildcardMatcher = {
    /**
     * Matches any single directory segment unless it is the last segment and a .min.js file
     * Breakdown:
     *  [^./]                   # matches everything up to the first . character (excluding directory separators)
     *  (\\.(?!min\\.js$))?     # matches . characters but not if they are part of the .min.js file extension
     */
    singleAsteriskRegexFragment: "([^./]|(\\.(?!min\\.js$))?)*",
    /**
     * Regex for the ** wildcard. Matches any number of subdirectories. When used for including
     * files or directories, does not match subdirectories that start with a . character
     */
    doubleAsteriskRegexFragment: `(/${implicitExcludePathRegexPattern}[^/.][^/]*)*?`,
    replaceWildcardCharacter: match => replaceWildcardCharacter(match, filesMatcher.singleAsteriskRegexFragment),
};

const directoriesMatcher: WildcardMatcher = {
    singleAsteriskRegexFragment: "[^/]*",
    /**
     * Regex for the ** wildcard. Matches any number of subdirectories. When used for including
     * files or directories, does not match subdirectories that start with a . character
     */
    doubleAsteriskRegexFragment: `(/${implicitExcludePathRegexPattern}[^/.][^/]*)*?`,
    replaceWildcardCharacter: match => replaceWildcardCharacter(match, directoriesMatcher.singleAsteriskRegexFragment),
};

const excludeMatcher: WildcardMatcher = {
    singleAsteriskRegexFragment: "[^/]*",
    doubleAsteriskRegexFragment: "(/.+?)?",
    replaceWildcardCharacter: match => replaceWildcardCharacter(match, excludeMatcher.singleAsteriskRegexFragment),
};

const wildcardMatchers = {
    files: filesMatcher,
    directories: directoriesMatcher,
    exclude: excludeMatcher,
};

/** @internal */
export function getRegularExpressionForWildcard(specs: readonly string[] | undefined, basePath: string, usage: "files" | "directories" | "exclude"): string | undefined {
    const patterns = getRegularExpressionsForWildcards(specs, basePath, usage);
    if (!patterns || !patterns.length) {
        return undefined;
    }

    const pattern = patterns.map(pattern => `(${pattern})`).join("|");
    // If excluding, match "foo/bar/baz...", but if including, only allow "foo".
    const terminator = usage === "exclude" ? "($|/)" : "$";
    return `^(${pattern})${terminator}`;
}

/** @internal */
export function getRegularExpressionsForWildcards(specs: readonly string[] | undefined, basePath: string, usage: "files" | "directories" | "exclude"): readonly string[] | undefined {
    if (specs === undefined || specs.length === 0) {
        return undefined;
    }

    return flatMap(specs, spec => spec && getSubPatternFromSpec(spec, basePath, usage, wildcardMatchers[usage]));
}

/**
 * An "includes" path "foo" is implicitly a glob "foo/** /*" (without the space) if its last component has no extension,
 * and does not contain any glob characters itself.
 *
 * @internal
 */
export function isImplicitGlob(lastPathComponent: string): boolean {
    return !/[.*?]/.test(lastPathComponent);
}

/** @internal */
export function getPatternFromSpec(spec: string, basePath: string, usage: "files" | "directories" | "exclude") {
    const pattern = spec && getSubPatternFromSpec(spec, basePath, usage, wildcardMatchers[usage]);
    return pattern && `^(${pattern})${usage === "exclude" ? "($|/)" : "$"}`;
}

/** @internal */
export function getSubPatternFromSpec(
    spec: string,
    basePath: string,
    usage: "files" | "directories" | "exclude",
    { singleAsteriskRegexFragment, doubleAsteriskRegexFragment, replaceWildcardCharacter }: WildcardMatcher = wildcardMatchers[usage],
): string | undefined {
    let subpattern = "";
    let hasWrittenComponent = false;
    const components = getNormalizedPathComponents(spec, basePath);
    const lastComponent = last(components);
    if (usage !== "exclude" && lastComponent === "**") {
        return undefined;
    }

    // getNormalizedPathComponents includes the separator for the root component.
    // We need to remove to create our regex correctly.
    components[0] = removeTrailingDirectorySeparator(components[0]);

    if (isImplicitGlob(lastComponent)) {
        components.push("**", "*");
    }

    let optionalCount = 0;
    for (let component of components) {
        if (component === "**") {
            subpattern += doubleAsteriskRegexFragment;
        }
        else {
            if (usage === "directories") {
                subpattern += "(";
                optionalCount++;
            }

            if (hasWrittenComponent) {
                subpattern += directorySeparator;
            }

            if (usage !== "exclude") {
                let componentPattern = "";
                // The * and ? wildcards should not match directories or files that start with . if they
                // appear first in a component. Dotted directories and files can be included explicitly
                // like so: **/.*/.*
                if (component.charCodeAt(0) === CharacterCodes.asterisk) {
                    componentPattern += "([^./]" + singleAsteriskRegexFragment + ")?";
                    component = component.substr(1);
                }
                else if (component.charCodeAt(0) === CharacterCodes.question) {
                    componentPattern += "[^./]";
                    component = component.substr(1);
                }

                componentPattern += component.replace(reservedCharacterPattern, replaceWildcardCharacter);

                // Patterns should not include subfolders like node_modules unless they are
                // explicitly included as part of the path.
                //
                // As an optimization, if the component pattern is the same as the component,
                // then there definitely were no wildcard characters and we do not need to
                // add the exclusion pattern.
                if (componentPattern !== component) {
                    subpattern += implicitExcludePathRegexPattern;
                }

                subpattern += componentPattern;
            }
            else {
                subpattern += component.replace(reservedCharacterPattern, replaceWildcardCharacter);
            }
        }

        hasWrittenComponent = true;
    }

    while (optionalCount > 0) {
        subpattern += ")?";
        optionalCount--;
    }

    return subpattern;
}

function replaceWildcardCharacter(match: string, singleAsteriskRegexFragment: string) {
    return match === "*" ? singleAsteriskRegexFragment : match === "?" ? "[^/]" : "\\" + match;
}

/** @internal */
export interface FileSystemEntries {
    readonly files: readonly string[];
    readonly directories: readonly string[];
}

/** @internal */
export interface FileMatcherPatterns {
    /** One pattern for each "include" spec. */
    includeFilePatterns: readonly string[] | undefined;
    /** One pattern matching one of any of the "include" specs. */
    includeFilePattern: string | undefined;
    includeDirectoryPattern: string | undefined;
    excludePattern: string | undefined;
    basePaths: readonly string[];
}

/**
 * @param path directory of the tsconfig.json
 *
 * @internal
 */
export function getFileMatcherPatterns(path: string, excludes: readonly string[] | undefined, includes: readonly string[] | undefined, useCaseSensitiveFileNames: boolean, currentDirectory: string): FileMatcherPatterns {
    path = normalizePath(path);
    currentDirectory = normalizePath(currentDirectory);
    const absolutePath = combinePaths(currentDirectory, path);

    return {
        includeFilePatterns: map(getRegularExpressionsForWildcards(includes, absolutePath, "files"), pattern => `^${pattern}$`),
        includeFilePattern: getRegularExpressionForWildcard(includes, absolutePath, "files"),
        includeDirectoryPattern: getRegularExpressionForWildcard(includes, absolutePath, "directories"),
        excludePattern: getRegularExpressionForWildcard(excludes, absolutePath, "exclude"),
        basePaths: getBasePaths(path, includes, useCaseSensitiveFileNames),
    };
}

/** @internal */
export function getRegexFromPattern(pattern: string, useCaseSensitiveFileNames: boolean): RegExp {
    return new RegExp(pattern, useCaseSensitiveFileNames ? "" : "i");
}

/**
 * @param path directory of the tsconfig.json
 *
 * @internal
 */
export function matchFiles(path: string, extensions: readonly string[] | undefined, excludes: readonly string[] | undefined, includes: readonly string[] | undefined, useCaseSensitiveFileNames: boolean, currentDirectory: string, depth: number | undefined, getFileSystemEntries: (path: string) => FileSystemEntries, realpath: (path: string) => string): string[] {
    path = normalizePath(path);
    currentDirectory = normalizePath(currentDirectory);

    const patterns = getFileMatcherPatterns(path, excludes, includes, useCaseSensitiveFileNames, currentDirectory);

    const includeFileRegexes = patterns.includeFilePatterns && patterns.includeFilePatterns.map(pattern => getRegexFromPattern(pattern, useCaseSensitiveFileNames));
    const includeDirectoryRegex = patterns.includeDirectoryPattern && getRegexFromPattern(patterns.includeDirectoryPattern, useCaseSensitiveFileNames);
    const excludeRegex = patterns.excludePattern && getRegexFromPattern(patterns.excludePattern, useCaseSensitiveFileNames);

    // Associate an array of results with each include regex. This keeps results in order of the "include" order.
    // If there are no "includes", then just put everything in results[0].
    const results: string[][] = includeFileRegexes ? includeFileRegexes.map(() => []) : [[]];
    const visited = new Map<string, true>();
    const toCanonical = createGetCanonicalFileName(useCaseSensitiveFileNames);
    for (const basePath of patterns.basePaths) {
        visitDirectory(basePath, combinePaths(currentDirectory, basePath), depth);
    }

    return flatten(results);

    function visitDirectory(path: string, absolutePath: string, depth: number | undefined) {
        const canonicalPath = toCanonical(realpath(absolutePath));
        if (visited.has(canonicalPath)) return;
        visited.set(canonicalPath, true);
        const { files, directories } = getFileSystemEntries(path);

        for (const current of sort<string>(files, compareStringsCaseSensitive)) {
            const name = combinePaths(path, current);
            const absoluteName = combinePaths(absolutePath, current);
            if (extensions && !fileExtensionIsOneOf(name, extensions)) continue;
            if (excludeRegex && excludeRegex.test(absoluteName)) continue;
            if (!includeFileRegexes) {
                results[0].push(name);
            }
            else {
                const includeIndex = findIndex(includeFileRegexes, re => re.test(absoluteName));
                if (includeIndex !== -1) {
                    results[includeIndex].push(name);
                }
            }
        }

        if (depth !== undefined) {
            depth--;
            if (depth === 0) {
                return;
            }
        }

        for (const current of sort<string>(directories, compareStringsCaseSensitive)) {
            const name = combinePaths(path, current);
            const absoluteName = combinePaths(absolutePath, current);
            if (
                (!includeDirectoryRegex || includeDirectoryRegex.test(absoluteName)) &&
                (!excludeRegex || !excludeRegex.test(absoluteName))
            ) {
                visitDirectory(name, absoluteName, depth);
            }
        }
    }
}

/**
 * Computes the unique non-wildcard base paths amongst the provided include patterns.
 */
function getBasePaths(path: string, includes: readonly string[] | undefined, useCaseSensitiveFileNames: boolean): string[] {
    // Storage for our results in the form of literal paths (e.g. the paths as written by the user).
    const basePaths: string[] = [path];

    if (includes) {
        // Storage for literal base paths amongst the include patterns.
        const includeBasePaths: string[] = [];
        for (const include of includes) {
            // We also need to check the relative paths by converting them to absolute and normalizing
            // in case they escape the base path (e.g "..\somedirectory")
            const absolute: string = isRootedDiskPath(include) ? include : normalizePath(combinePaths(path, include));
            // Append the literal and canonical candidate base paths.
            includeBasePaths.push(getIncludeBasePath(absolute));
        }

        // Sort the offsets array using either the literal or canonical path representations.
        includeBasePaths.sort(getStringComparer(!useCaseSensitiveFileNames));

        // Iterate over each include base path and include unique base paths that are not a
        // subpath of an existing base path
        for (const includeBasePath of includeBasePaths) {
            if (every(basePaths, basePath => !containsPath(basePath, includeBasePath, path, !useCaseSensitiveFileNames))) {
                basePaths.push(includeBasePath);
            }
        }
    }

    return basePaths;
}

function getIncludeBasePath(absolute: string): string {
    const wildcardOffset = indexOfAnyCharCode(absolute, wildcardCharCodes);
    if (wildcardOffset < 0) {
        // No "*" or "?" in the path
        return !hasExtension(absolute)
            ? absolute
            : removeTrailingDirectorySeparator(getDirectoryPath(absolute));
    }
    return absolute.substring(0, absolute.lastIndexOf(directorySeparator, wildcardOffset));
}

/** @internal */
export function ensureScriptKind(fileName: string, scriptKind: ScriptKind | undefined): ScriptKind {
    // Using scriptKind as a condition handles both:
    // - 'scriptKind' is unspecified and thus it is `undefined`
    // - 'scriptKind' is set and it is `Unknown` (0)
    // If the 'scriptKind' is 'undefined' or 'Unknown' then we attempt
    // to get the ScriptKind from the file name. If it cannot be resolved
    // from the file name then the default 'TS' script kind is returned.
    return scriptKind || getScriptKindFromFileName(fileName) || ScriptKind.TS;
}

/** @internal */
export function getScriptKindFromFileName(fileName: string): ScriptKind {
    const ext = fileName.substr(fileName.lastIndexOf("."));
    switch (ext.toLowerCase()) {
        case Extension.Js:
        case Extension.Cjs:
        case Extension.Mjs:
            return ScriptKind.JS;
        case Extension.Jsx:
            return ScriptKind.JSX;
        case Extension.Ts:
        case Extension.Cts:
        case Extension.Mts:
            return ScriptKind.TS;
        case Extension.Tsx:
            return ScriptKind.TSX;
        case Extension.Json:
            return ScriptKind.JSON;
        default:
            return ScriptKind.Unknown;
    }
}

/**
 *  Groups of supported extensions in order of file resolution precedence. (eg, TS > TSX > DTS and seperately, CTS > DCTS)
 *
 * @internal
 */
export const supportedTSExtensions: readonly Extension[][] = [[Extension.Ts, Extension.Tsx, Extension.Dts], [Extension.Cts, Extension.Dcts], [Extension.Mts, Extension.Dmts]];
/** @internal */
export const supportedTSExtensionsFlat: readonly Extension[] = flatten(supportedTSExtensions);
const supportedTSExtensionsWithJson: readonly Extension[][] = [...supportedTSExtensions, [Extension.Json]];
/** Must have ".d.ts" first because if ".ts" goes first, that will be detected as the extension instead of ".d.ts". */
const supportedTSExtensionsForExtractExtension: readonly Extension[] = [Extension.Dts, Extension.Dcts, Extension.Dmts, Extension.Cts, Extension.Mts, Extension.Ts, Extension.Tsx];
/** @internal */
export const supportedJSExtensions: readonly Extension[][] = [[Extension.Js, Extension.Jsx], [Extension.Mjs], [Extension.Cjs]];
/** @internal */
export const supportedJSExtensionsFlat: readonly Extension[] = flatten(supportedJSExtensions);
const allSupportedExtensions: readonly Extension[][] = [[Extension.Ts, Extension.Tsx, Extension.Dts, Extension.Js, Extension.Jsx], [Extension.Cts, Extension.Dcts, Extension.Cjs], [Extension.Mts, Extension.Dmts, Extension.Mjs]];
const allSupportedExtensionsWithJson: readonly Extension[][] = [...allSupportedExtensions, [Extension.Json]];
/** @internal */
export const supportedDeclarationExtensions: readonly Extension[] = [Extension.Dts, Extension.Dcts, Extension.Dmts];
/** @internal */
export const supportedTSImplementationExtensions: readonly Extension[] = [Extension.Ts, Extension.Cts, Extension.Mts, Extension.Tsx];
/** @internal */
export const extensionsNotSupportingExtensionlessResolution: readonly Extension[] = [Extension.Mts, Extension.Dmts, Extension.Mjs, Extension.Cts, Extension.Dcts, Extension.Cjs];

/** @internal */
export function getSupportedExtensions(options?: CompilerOptions): readonly Extension[][];
/** @internal */
export function getSupportedExtensions(options?: CompilerOptions, extraFileExtensions?: readonly FileExtensionInfo[]): readonly string[][];
/** @internal */
export function getSupportedExtensions(options?: CompilerOptions, extraFileExtensions?: readonly FileExtensionInfo[]): readonly string[][] {
    const needJsExtensions = options && getAllowJSCompilerOption(options);

    if (!extraFileExtensions || extraFileExtensions.length === 0) {
        return needJsExtensions ? allSupportedExtensions : supportedTSExtensions;
    }

    const builtins = needJsExtensions ? allSupportedExtensions : supportedTSExtensions;
    const flatBuiltins = flatten(builtins);
    const extensions = [
        ...builtins,
        ...mapDefined(extraFileExtensions, x => x.scriptKind === ScriptKind.Deferred || needJsExtensions && isJSLike(x.scriptKind) && !flatBuiltins.includes(x.extension as Extension) ? [x.extension] : undefined),
    ];

    return extensions;
}

/** @internal */
export function getSupportedExtensionsWithJsonIfResolveJsonModule(options: CompilerOptions | undefined, supportedExtensions: readonly Extension[][]): readonly Extension[][];
/** @internal */
export function getSupportedExtensionsWithJsonIfResolveJsonModule(options: CompilerOptions | undefined, supportedExtensions: readonly string[][]): readonly string[][];
/** @internal */
export function getSupportedExtensionsWithJsonIfResolveJsonModule(options: CompilerOptions | undefined, supportedExtensions: readonly string[][]): readonly string[][] {
    if (!options || !getResolveJsonModule(options)) return supportedExtensions;
    if (supportedExtensions === allSupportedExtensions) return allSupportedExtensionsWithJson;
    if (supportedExtensions === supportedTSExtensions) return supportedTSExtensionsWithJson;
    return [...supportedExtensions, [Extension.Json]];
}

function isJSLike(scriptKind: ScriptKind | undefined): boolean {
    return scriptKind === ScriptKind.JS || scriptKind === ScriptKind.JSX;
}

/** @internal */
export function hasJSFileExtension(fileName: string): boolean {
    return some(supportedJSExtensionsFlat, extension => fileExtensionIs(fileName, extension));
}

/** @internal */
export function hasTSFileExtension(fileName: string): boolean {
    return some(supportedTSExtensionsFlat, extension => fileExtensionIs(fileName, extension));
}

/**
 * @internal
 * Corresponds to UserPreferences#importPathEnding
 */
export const enum ModuleSpecifierEnding {
    Minimal,
    Index,
    JsExtension,
    TsExtension,
}

/** @internal */
export function usesExtensionsOnImports({ imports }: SourceFile, hasExtension: (text: string) => boolean = or(hasJSFileExtension, hasTSFileExtension)): boolean {
    return firstDefined(imports, ({ text }) =>
        pathIsRelative(text) && !fileExtensionIsOneOf(text, extensionsNotSupportingExtensionlessResolution)
            ? hasExtension(text)
            : undefined) || false;
}

/** @internal */
export function getModuleSpecifierEndingPreference(preference: UserPreferences["importModuleSpecifierEnding"], resolutionMode: ResolutionMode, compilerOptions: CompilerOptions, sourceFile: SourceFile): ModuleSpecifierEnding {
    const moduleResolution = getEmitModuleResolutionKind(compilerOptions);
    const moduleResolutionIsNodeNext = ModuleResolutionKind.Node16 <= moduleResolution && moduleResolution <= ModuleResolutionKind.NodeNext;
    if (preference === "js" || resolutionMode === ModuleKind.ESNext && moduleResolutionIsNodeNext) {
        // Extensions are explicitly requested or required. Now choose between .js and .ts.
        if (!shouldAllowImportingTsExtension(compilerOptions)) {
            return ModuleSpecifierEnding.JsExtension;
        }
        // `allowImportingTsExtensions` is a strong signal, so use .ts unless the file
        // already uses .js extensions and no .ts extensions.
        return inferPreference() !== ModuleSpecifierEnding.JsExtension
            ? ModuleSpecifierEnding.TsExtension
            : ModuleSpecifierEnding.JsExtension;
    }
    if (preference === "minimal") {
        return ModuleSpecifierEnding.Minimal;
    }
    if (preference === "index") {
        return ModuleSpecifierEnding.Index;
    }

    // No preference was specified.
    // Look at imports and/or requires to guess whether .js, .ts, or extensionless imports are preferred.
    // N.B. that `Index` detection is not supported since it would require file system probing to do
    // accurately, and more importantly, literally nobody wants `Index` and its existence is a mystery.
    if (!shouldAllowImportingTsExtension(compilerOptions)) {
        // If .ts imports are not valid, we only need to see one .js import to go with that.
        return usesExtensionsOnImports(sourceFile) ? ModuleSpecifierEnding.JsExtension : ModuleSpecifierEnding.Minimal;
    }

    return inferPreference();

    function inferPreference() {
        let usesJsExtensions = false;
        const specifiers = sourceFile.imports.length ? sourceFile.imports :
            isSourceFileJS(sourceFile) ? getRequiresAtTopOfFile(sourceFile).map(r => r.arguments[0]) :
            emptyArray;
        for (const specifier of specifiers) {
            if (pathIsRelative(specifier.text)) {
                if (
                    moduleResolutionIsNodeNext &&
                    resolutionMode === ModuleKind.CommonJS &&
                    getModeForUsageLocation(sourceFile, specifier, compilerOptions) === ModuleKind.ESNext
                ) {
                    // We're trying to decide a preference for a CommonJS module specifier, but looking at an ESM import.
                    continue;
                }
                if (fileExtensionIsOneOf(specifier.text, extensionsNotSupportingExtensionlessResolution)) {
                    // These extensions are not optional, so do not indicate a preference.
                    continue;
                }
                if (hasTSFileExtension(specifier.text)) {
                    return ModuleSpecifierEnding.TsExtension;
                }
                if (hasJSFileExtension(specifier.text)) {
                    usesJsExtensions = true;
                }
            }
        }
        return usesJsExtensions ? ModuleSpecifierEnding.JsExtension : ModuleSpecifierEnding.Minimal;
    }
}

function getRequiresAtTopOfFile(sourceFile: SourceFile): readonly RequireOrImportCall[] {
    let nonRequireStatementCount = 0;
    let requires: RequireOrImportCall[] | undefined;
    for (const statement of sourceFile.statements) {
        if (nonRequireStatementCount > 3) {
            break;
        }
        if (isRequireVariableStatement(statement)) {
            requires = concatenate(requires, statement.declarationList.declarations.map(d => d.initializer));
        }
        else if (isExpressionStatement(statement) && isRequireCall(statement.expression, /*requireStringLiteralLikeArgument*/ true)) {
            requires = append(requires, statement.expression);
        }
        else {
            nonRequireStatementCount++;
        }
    }
    return requires || emptyArray;
}

/** @internal */
export function isSupportedSourceFileName(fileName: string, compilerOptions?: CompilerOptions, extraFileExtensions?: readonly FileExtensionInfo[]) {
    if (!fileName) return false;

    const supportedExtensions = getSupportedExtensions(compilerOptions, extraFileExtensions);
    for (const extension of flatten(getSupportedExtensionsWithJsonIfResolveJsonModule(compilerOptions, supportedExtensions))) {
        if (fileExtensionIs(fileName, extension)) {
            return true;
        }
    }
    return false;
}

function numberOfDirectorySeparators(str: string) {
    const match = str.match(/\//g);
    return match ? match.length : 0;
}

/** @internal */
export function compareNumberOfDirectorySeparators(path1: string, path2: string) {
    return compareValues(
        numberOfDirectorySeparators(path1),
        numberOfDirectorySeparators(path2),
    );
}

const extensionsToRemove = [Extension.Dts, Extension.Dmts, Extension.Dcts, Extension.Mjs, Extension.Mts, Extension.Cjs, Extension.Cts, Extension.Ts, Extension.Js, Extension.Tsx, Extension.Jsx, Extension.Json];
/** @internal */
export function removeFileExtension(path: string): string {
    for (const ext of extensionsToRemove) {
        const extensionless = tryRemoveExtension(path, ext);
        if (extensionless !== undefined) {
            return extensionless;
        }
    }
    return path;
}

/** @internal */
export function tryRemoveExtension(path: string, extension: string): string | undefined {
    return fileExtensionIs(path, extension) ? removeExtension(path, extension) : undefined;
}

/** @internal */
export function removeExtension(path: string, extension: string): string {
    return path.substring(0, path.length - extension.length);
}

/** @internal */
export function changeExtension<T extends string | Path>(path: T, newExtension: string): T {
    return changeAnyExtension(path, newExtension, extensionsToRemove, /*ignoreCase*/ false) as T;
}

/**
 * Returns the input if there are no stars, a pattern if there is exactly one,
 * and undefined if there are more.
 *
 * @internal
 */
export function tryParsePattern(pattern: string): string | Pattern | undefined {
    const indexOfStar = pattern.indexOf("*");
    if (indexOfStar === -1) {
        return pattern;
    }
    return pattern.indexOf("*", indexOfStar + 1) !== -1
        ? undefined
        : {
            prefix: pattern.substr(0, indexOfStar),
            suffix: pattern.substr(indexOfStar + 1),
        };
}

/** @internal */
export function tryParsePatterns(paths: MapLike<string[]>): (string | Pattern)[] {
    return mapDefined(getOwnKeys(paths), path => tryParsePattern(path));
}

/** @internal */
export function positionIsSynthesized(pos: number): boolean {
    // This is a fast way of testing the following conditions:
    //  pos === undefined || pos === null || isNaN(pos) || pos < 0;
    return !(pos >= 0);
}

/**
 * True if an extension is one of the supported TypeScript extensions.
 *
 * @internal
 */
export function extensionIsTS(ext: string): boolean {
    return ext === Extension.Ts || ext === Extension.Tsx || ext === Extension.Dts || ext === Extension.Cts || ext === Extension.Mts || ext === Extension.Dmts || ext === Extension.Dcts || (startsWith(ext, ".d.") && endsWith(ext, ".ts"));
}

/** @internal */
export function resolutionExtensionIsTSOrJson(ext: string) {
    return extensionIsTS(ext) || ext === Extension.Json;
}

/**
 * Gets the extension from a path.
 * Path must have a valid extension.
 *
 * @internal
 */
export function extensionFromPath(path: string): Extension {
    const ext = tryGetExtensionFromPath(path);
    return ext !== undefined ? ext : Debug.fail(`File ${path} has unknown extension.`);
}

/** @internal */
export function isAnySupportedFileExtension(path: string): boolean {
    return tryGetExtensionFromPath(path) !== undefined;
}

/** @internal */
export function tryGetExtensionFromPath(path: string): Extension | undefined {
    return find(extensionsToRemove, e => fileExtensionIs(path, e));
}

/** @internal */
export function isCheckJsEnabledForFile(sourceFile: SourceFile, compilerOptions: CompilerOptions) {
    return sourceFile.checkJsDirective ? sourceFile.checkJsDirective.enabled : compilerOptions.checkJs;
}

/** @internal */
export const emptyFileSystemEntries: FileSystemEntries = {
    files: emptyArray,
    directories: emptyArray,
};

/**
 * patternOrStrings contains both patterns (containing "*") and regular strings.
 * Return an exact match if possible, or a pattern match, or undefined.
 * (These are verified by verifyCompilerOptions to have 0 or 1 "*" characters.)
 *
 * @internal
 */
export function matchPatternOrExact(patternOrStrings: readonly (string | Pattern)[], candidate: string): string | Pattern | undefined {
    const patterns: Pattern[] = [];
    for (const patternOrString of patternOrStrings) {
        if (patternOrString === candidate) {
            return candidate;
        }

        if (!isString(patternOrString)) {
            patterns.push(patternOrString);
        }
    }

    return findBestPatternMatch(patterns, _ => _, candidate);
}

/** @internal */
export type Mutable<T extends object> = { -readonly [K in keyof T]: T[K]; };

/** @internal */
export function sliceAfter<T>(arr: readonly T[], value: T): readonly T[] {
    const index = arr.indexOf(value);
    Debug.assert(index !== -1);
    return arr.slice(index);
}

/** @internal */
export function addRelatedInfo<T extends Diagnostic>(diagnostic: T, ...relatedInformation: DiagnosticRelatedInformation[]): T {
    if (!relatedInformation.length) {
        return diagnostic;
    }
    if (!diagnostic.relatedInformation) {
        diagnostic.relatedInformation = [];
    }
    Debug.assert(diagnostic.relatedInformation !== emptyArray, "Diagnostic had empty array singleton for related info, but is still being constructed!");
    diagnostic.relatedInformation.push(...relatedInformation);
    return diagnostic;
}

/** @internal */
export function minAndMax<T>(arr: readonly T[], getValue: (value: T) => number): { readonly min: number; readonly max: number; } {
    Debug.assert(arr.length !== 0);
    let min = getValue(arr[0]);
    let max = min;
    for (let i = 1; i < arr.length; i++) {
        const value = getValue(arr[i]);
        if (value < min) {
            min = value;
        }
        else if (value > max) {
            max = value;
        }
    }
    return { min, max };
}

/** @internal */
export function rangeOfNode(node: Node): TextRange {
    return { pos: getTokenPosOfNode(node), end: node.end };
}

/** @internal */
export function rangeOfTypeParameters(sourceFile: SourceFile, typeParameters: NodeArray<TypeParameterDeclaration>): TextRange {
    // Include the `<>`
    const pos = typeParameters.pos - 1;
    const end = Math.min(sourceFile.text.length, skipTrivia(sourceFile.text, typeParameters.end) + 1);
    return { pos, end };
}

/** @internal */
export interface HostWithIsSourceOfProjectReferenceRedirect {
    isSourceOfProjectReferenceRedirect(fileName: string): boolean;
}
/** @internal */
export function skipTypeChecking(sourceFile: SourceFile, options: CompilerOptions, host: HostWithIsSourceOfProjectReferenceRedirect) {
    // If skipLibCheck is enabled, skip reporting errors if file is a declaration file.
    // If skipDefaultLibCheck is enabled, skip reporting errors if file contains a
    // '/// <reference no-default-lib="true"/>' directive.
    return (options.skipLibCheck && sourceFile.isDeclarationFile ||
        options.skipDefaultLibCheck && sourceFile.hasNoDefaultLib) ||
        host.isSourceOfProjectReferenceRedirect(sourceFile.fileName);
}

/** @internal */
export function isJsonEqual(a: unknown, b: unknown): boolean {
    // eslint-disable-next-line no-null/no-null
    return a === b || typeof a === "object" && a !== null && typeof b === "object" && b !== null && equalOwnProperties(a as MapLike<unknown>, b as MapLike<unknown>, isJsonEqual);
}

/**
 * Converts a bigint literal string, e.g. `0x1234n`,
 * to its decimal string representation, e.g. `4660`.
 *
 * @internal
 */
export function parsePseudoBigInt(stringValue: string): string {
    let log2Base: number;
    switch (stringValue.charCodeAt(1)) { // "x" in "0x123"
        case CharacterCodes.b:
        case CharacterCodes.B: // 0b or 0B
            log2Base = 1;
            break;
        case CharacterCodes.o:
        case CharacterCodes.O: // 0o or 0O
            log2Base = 3;
            break;
        case CharacterCodes.x:
        case CharacterCodes.X: // 0x or 0X
            log2Base = 4;
            break;
        default: // already in decimal; omit trailing "n"
            const nIndex = stringValue.length - 1;
            // Skip leading 0s
            let nonZeroStart = 0;
            while (stringValue.charCodeAt(nonZeroStart) === CharacterCodes._0) {
                nonZeroStart++;
            }
            return stringValue.slice(nonZeroStart, nIndex) || "0";
    }

    // Omit leading "0b", "0o", or "0x", and trailing "n"
    const startIndex = 2, endIndex = stringValue.length - 1;
    const bitsNeeded = (endIndex - startIndex) * log2Base;
    // Stores the value specified by the string as a LE array of 16-bit integers
    // using Uint16 instead of Uint32 so combining steps can use bitwise operators
    const segments = new Uint16Array((bitsNeeded >>> 4) + (bitsNeeded & 15 ? 1 : 0));
    // Add the digits, one at a time
    for (let i = endIndex - 1, bitOffset = 0; i >= startIndex; i--, bitOffset += log2Base) {
        const segment = bitOffset >>> 4;
        const digitChar = stringValue.charCodeAt(i);
        // Find character range: 0-9 < A-F < a-f
        const digit = digitChar <= CharacterCodes._9
            ? digitChar - CharacterCodes._0
            : 10 + digitChar -
                (digitChar <= CharacterCodes.F ? CharacterCodes.A : CharacterCodes.a);
        const shiftedDigit = digit << (bitOffset & 15);
        segments[segment] |= shiftedDigit;
        const residual = shiftedDigit >>> 16;
        if (residual) segments[segment + 1] |= residual; // overflows segment
    }
    // Repeatedly divide segments by 10 and add remainder to base10Value
    let base10Value = "";
    let firstNonzeroSegment = segments.length - 1;
    let segmentsRemaining = true;
    while (segmentsRemaining) {
        let mod10 = 0;
        segmentsRemaining = false;
        for (let segment = firstNonzeroSegment; segment >= 0; segment--) {
            const newSegment = mod10 << 16 | segments[segment];
            const segmentValue = (newSegment / 10) | 0;
            segments[segment] = segmentValue;
            mod10 = newSegment - segmentValue * 10;
            if (segmentValue && !segmentsRemaining) {
                firstNonzeroSegment = segment;
                segmentsRemaining = true;
            }
        }
        base10Value = mod10 + base10Value;
    }
    return base10Value;
}

/** @internal */
export function pseudoBigIntToString({ negative, base10Value }: PseudoBigInt): string {
    return (negative && base10Value !== "0" ? "-" : "") + base10Value;
}

/** @internal */
export function parseBigInt(text: string): PseudoBigInt | undefined {
    if (!isValidBigIntString(text, /*roundTripOnly*/ false)) {
        return undefined;
    }
    return parseValidBigInt(text);
}

/**
 * @internal
 * @param text a valid bigint string excluding a trailing `n`, but including a possible prefix `-`. Use `isValidBigIntString(text, roundTripOnly)` before calling this function.
 */
export function parseValidBigInt(text: string): PseudoBigInt {
    const negative = text.startsWith("-");
    const base10Value = parsePseudoBigInt(`${negative ? text.slice(1) : text}n`);
    return { negative, base10Value };
}

/**
 * @internal
 * Tests whether the provided string can be parsed as a bigint.
 * @param s The string to test.
 * @param roundTripOnly Indicates the resulting bigint matches the input when converted back to a string.
 */
export function isValidBigIntString(s: string, roundTripOnly: boolean): boolean {
    if (s === "") return false;
    const scanner = createScanner(ScriptTarget.ESNext, /*skipTrivia*/ false);
    let success = true;
    scanner.setOnError(() => success = false);
    scanner.setText(s + "n");
    let result = scanner.scan();
    const negative = result === SyntaxKind.MinusToken;
    if (negative) {
        result = scanner.scan();
    }
    const flags = scanner.getTokenFlags();
    // validate that
    // * scanning proceeded without error
    // * a bigint can be scanned, and that when it is scanned, it is
    // * the full length of the input string (so the scanner is one character beyond the augmented input length)
    // * it does not contain a numeric seperator (the `BigInt` constructor does not accept a numeric seperator in its input)
    return success && result === SyntaxKind.BigIntLiteral && scanner.getTokenEnd() === (s.length + 1) && !(flags & TokenFlags.ContainsSeparator)
        && (!roundTripOnly || s === pseudoBigIntToString({ negative, base10Value: parsePseudoBigInt(scanner.getTokenValue()) }));
}

/** @internal */
export function isValidTypeOnlyAliasUseSite(useSite: Node): boolean {
    return !!(useSite.flags & NodeFlags.Ambient)
        || isPartOfTypeQuery(useSite)
        || isIdentifierInNonEmittingHeritageClause(useSite)
        || isPartOfPossiblyValidTypeOrAbstractComputedPropertyName(useSite)
        || !(isExpressionNode(useSite) || isShorthandPropertyNameUseSite(useSite));
}

function isShorthandPropertyNameUseSite(useSite: Node) {
    return isIdentifier(useSite) && isShorthandPropertyAssignment(useSite.parent) && useSite.parent.name === useSite;
}

function isPartOfPossiblyValidTypeOrAbstractComputedPropertyName(node: Node) {
    while (node.kind === SyntaxKind.Identifier || node.kind === SyntaxKind.PropertyAccessExpression) {
        node = node.parent;
    }
    if (node.kind !== SyntaxKind.ComputedPropertyName) {
        return false;
    }
    if (hasSyntacticModifier(node.parent, ModifierFlags.Abstract)) {
        return true;
    }
    const containerKind = node.parent.parent.kind;
    return containerKind === SyntaxKind.InterfaceDeclaration || containerKind === SyntaxKind.TypeLiteral;
}

/** Returns true for an identifier in 1) an `implements` clause, and 2) an `extends` clause of an interface. */
function isIdentifierInNonEmittingHeritageClause(node: Node): boolean {
    if (node.kind !== SyntaxKind.Identifier) return false;
    const heritageClause = findAncestor(node.parent, parent => {
        switch (parent.kind) {
            case SyntaxKind.HeritageClause:
                return true;
            case SyntaxKind.PropertyAccessExpression:
            case SyntaxKind.ExpressionWithTypeArguments:
                return false;
            default:
                return "quit";
        }
    }) as HeritageClause | undefined;
    return heritageClause?.token === SyntaxKind.ImplementsKeyword || heritageClause?.parent.kind === SyntaxKind.InterfaceDeclaration;
}

/** @internal */
export function isIdentifierTypeReference(node: Node): node is TypeReferenceNode & { typeName: Identifier; } {
    return isTypeReferenceNode(node) && isIdentifier(node.typeName);
}

/** @internal */
export function arrayIsHomogeneous<T>(array: readonly T[], comparer: EqualityComparer<T> = equateValues) {
    if (array.length < 2) return true;
    const first = array[0];
    for (let i = 1, length = array.length; i < length; i++) {
        const target = array[i];
        if (!comparer(first, target)) return false;
    }
    return true;
}

/**
 * Bypasses immutability and directly sets the `pos` property of a `TextRange` or `Node`.
 *
 * @internal
 */
export function setTextRangePos<T extends ReadonlyTextRange>(range: T, pos: number) {
    (range as TextRange).pos = pos;
    return range;
}

/**
 * Bypasses immutability and directly sets the `end` property of a `TextRange` or `Node`.
 *
 * @internal
 */
export function setTextRangeEnd<T extends ReadonlyTextRange>(range: T, end: number) {
    (range as TextRange).end = end;
    return range;
}

/**
 * Bypasses immutability and directly sets the `pos` and `end` properties of a `TextRange` or `Node`.
 *
 * @internal
 */
export function setTextRangePosEnd<T extends ReadonlyTextRange>(range: T, pos: number, end: number) {
    return setTextRangeEnd(setTextRangePos(range, pos), end);
}

/**
 * Bypasses immutability and directly sets the `pos` and `end` properties of a `TextRange` or `Node` from the
 * provided position and width.
 *
 * @internal
 */
export function setTextRangePosWidth<T extends ReadonlyTextRange>(range: T, pos: number, width: number) {
    return setTextRangePosEnd(range, pos, pos + width);
}

/**
 * Bypasses immutability and directly sets the `flags` property of a `Node`.
 *
 * @internal
 */
export function setNodeFlags<T extends Node>(node: T, newFlags: NodeFlags): T;
/** @internal */
export function setNodeFlags<T extends Node>(node: T | undefined, newFlags: NodeFlags): T | undefined;
/** @internal */
export function setNodeFlags<T extends Node>(node: T | undefined, newFlags: NodeFlags): T | undefined {
    if (node) {
        (node as Mutable<T>).flags = newFlags;
    }
    return node;
}

/**
 * Bypasses immutability and directly sets the `parent` property of a `Node`.
 *
 * @internal
 */
export function setParent<T extends Node>(child: T, parent: T["parent"] | undefined): T;
/** @internal */
export function setParent<T extends Node>(child: T | undefined, parent: T["parent"] | undefined): T | undefined;
/** @internal */
export function setParent<T extends Node>(child: T | undefined, parent: T["parent"] | undefined): T | undefined {
    if (child && parent) {
        (child as Mutable<T>).parent = parent;
    }
    return child;
}

/**
 * Bypasses immutability and directly sets the `parent` property of each `Node` in an array of nodes, if is not already set.
 *
 * @internal
 */
export function setEachParent<T extends readonly Node[]>(children: T, parent: T[number]["parent"]): T;
/** @internal */
export function setEachParent<T extends readonly Node[]>(children: T | undefined, parent: T[number]["parent"]): T | undefined;
/** @internal */
export function setEachParent<T extends readonly Node[]>(children: T | undefined, parent: T[number]["parent"]): T | undefined {
    if (children) {
        for (const child of children) {
            setParent(child, parent);
        }
    }
    return children;
}

/**
 * Bypasses immutability and directly sets the `parent` property of each `Node` recursively.
 * @param rootNode The root node from which to start the recursion.
 * @param incremental When `true`, only recursively descends through nodes whose `parent` pointers are incorrect.
 * This allows us to quickly bail out of setting `parent` for subtrees during incremental parsing.
 *
 * @internal
 */
export function setParentRecursive<T extends Node>(rootNode: T, incremental: boolean): T;
/** @internal */
export function setParentRecursive<T extends Node>(rootNode: T | undefined, incremental: boolean): T | undefined;
/** @internal */
export function setParentRecursive<T extends Node>(rootNode: T | undefined, incremental: boolean): T | undefined {
    if (!rootNode) return rootNode;
    forEachChildRecursively(rootNode, isJSDocNode(rootNode) ? bindParentToChildIgnoringJSDoc : bindParentToChild);
    return rootNode;

    function bindParentToChildIgnoringJSDoc(child: Node, parent: Node): void | "skip" {
        if (incremental && child.parent === parent) {
            return "skip";
        }
        setParent(child, parent);
    }

    function bindJSDoc(child: Node) {
        if (hasJSDocNodes(child)) {
            for (const doc of child.jsDoc!) {
                bindParentToChildIgnoringJSDoc(doc, child);
                forEachChildRecursively(doc, bindParentToChildIgnoringJSDoc);
            }
        }
    }

    function bindParentToChild(child: Node, parent: Node) {
        return bindParentToChildIgnoringJSDoc(child, parent) || bindJSDoc(child);
    }
}

function isPackedElement(node: Expression) {
    return !isOmittedExpression(node);
}

/**
 * Determines whether the provided node is an ArrayLiteralExpression that contains no missing elements.
 *
 * @internal
 */
export function isPackedArrayLiteral(node: Expression) {
    return isArrayLiteralExpression(node) && every(node.elements, isPackedElement);
}

/**
 * Indicates whether the result of an `Expression` will be unused.
 *
 * NOTE: This requires a node with a valid `parent` pointer.
 *
 * @internal
 */
export function expressionResultIsUnused(node: Expression): boolean {
    Debug.assertIsDefined(node.parent);
    while (true) {
        const parent: Node = node.parent;
        // walk up parenthesized expressions, but keep a pointer to the top-most parenthesized expression
        if (isParenthesizedExpression(parent)) {
            node = parent;
            continue;
        }
        // result is unused in an expression statement, `void` expression, or the initializer or incrementer of a `for` loop
        if (
            isExpressionStatement(parent) ||
            isVoidExpression(parent) ||
            isForStatement(parent) && (parent.initializer === node || parent.incrementor === node)
        ) {
            return true;
        }
        if (isCommaListExpression(parent)) {
            // left side of comma is always unused
            if (node !== last(parent.elements)) return true;
            // right side of comma is unused if parent is unused
            node = parent;
            continue;
        }
        if (isBinaryExpression(parent) && parent.operatorToken.kind === SyntaxKind.CommaToken) {
            // left side of comma is always unused
            if (node === parent.left) return true;
            // right side of comma is unused if parent is unused
            node = parent;
            continue;
        }
        return false;
    }
}

/** @internal */
export function containsIgnoredPath(path: string) {
    return some(ignoredPaths, p => path.includes(p));
}

/** @internal */
export function getContainingNodeArray(node: Node): NodeArray<Node> | undefined {
    if (!node.parent) return undefined;
    switch (node.kind) {
        case SyntaxKind.TypeParameter:
            const { parent } = node as TypeParameterDeclaration;
            return parent.kind === SyntaxKind.InferType ? undefined : parent.typeParameters;
        case SyntaxKind.Parameter:
            return (node as ParameterDeclaration).parent.parameters;
        case SyntaxKind.TemplateLiteralTypeSpan:
            return (node as TemplateLiteralTypeSpan).parent.templateSpans;
        case SyntaxKind.TemplateSpan:
            return (node as TemplateSpan).parent.templateSpans;
        case SyntaxKind.Decorator: {
            const { parent } = node as Decorator;
            return canHaveDecorators(parent) ? parent.modifiers :
                undefined;
        }
        case SyntaxKind.HeritageClause:
            return (node as HeritageClause).parent.heritageClauses;
    }

    const { parent } = node;
    if (isJSDocTag(node)) {
        return isJSDocTypeLiteral(node.parent) ? undefined : node.parent.tags;
    }

    switch (parent.kind) {
        case SyntaxKind.TypeLiteral:
        case SyntaxKind.InterfaceDeclaration:
            return isTypeElement(node) ? (parent as TypeLiteralNode | InterfaceDeclaration).members : undefined;
        case SyntaxKind.UnionType:
        case SyntaxKind.IntersectionType:
            return (parent as UnionOrIntersectionTypeNode).types;
        case SyntaxKind.TupleType:
        case SyntaxKind.ArrayLiteralExpression:
        case SyntaxKind.CommaListExpression:
        case SyntaxKind.NamedImports:
        case SyntaxKind.NamedExports:
            return (parent as TupleTypeNode | ArrayLiteralExpression | CommaListExpression | NamedImports | NamedExports).elements;
        case SyntaxKind.ObjectLiteralExpression:
        case SyntaxKind.JsxAttributes:
            return (parent as ObjectLiteralExpressionBase<ObjectLiteralElement>).properties;
        case SyntaxKind.CallExpression:
        case SyntaxKind.NewExpression:
            return isTypeNode(node) ? (parent as CallExpression | NewExpression).typeArguments :
                (parent as CallExpression | NewExpression).expression === node ? undefined :
                (parent as CallExpression | NewExpression).arguments;
        case SyntaxKind.JsxElement:
        case SyntaxKind.JsxFragment:
            return isJsxChild(node) ? (parent as JsxElement | JsxFragment).children : undefined;
        case SyntaxKind.JsxOpeningElement:
        case SyntaxKind.JsxSelfClosingElement:
            return isTypeNode(node) ? (parent as JsxOpeningElement | JsxSelfClosingElement).typeArguments : undefined;
        case SyntaxKind.Block:
        case SyntaxKind.CaseClause:
        case SyntaxKind.DefaultClause:
        case SyntaxKind.ModuleBlock:
            return (parent as Block | CaseOrDefaultClause | ModuleBlock).statements;
        case SyntaxKind.CaseBlock:
            return (parent as CaseBlock).clauses;
        case SyntaxKind.ClassDeclaration:
        case SyntaxKind.ClassExpression:
            return isClassElement(node) ? (parent as ClassLikeDeclaration).members : undefined;
        case SyntaxKind.EnumDeclaration:
            return isEnumMember(node) ? (parent as EnumDeclaration).members : undefined;
        case SyntaxKind.SourceFile:
            return (parent as SourceFile).statements;
    }
}

/** @internal */
export function hasContextSensitiveParameters(node: FunctionLikeDeclaration) {
    // Functions with type parameters are not context sensitive.
    if (!node.typeParameters) {
        // Functions with any parameters that lack type annotations are context sensitive.
        if (some(node.parameters, p => !getEffectiveTypeAnnotationNode(p))) {
            return true;
        }
        if (node.kind !== SyntaxKind.ArrowFunction) {
            // If the first parameter is not an explicit 'this' parameter, then the function has
            // an implicit 'this' parameter which is subject to contextual typing.
            const parameter = firstOrUndefined(node.parameters);
            if (!(parameter && parameterIsThisKeyword(parameter))) {
                return true;
            }
        }
    }
    return false;
}

/** @internal */
export function isInfinityOrNaNString(name: string | __String): boolean {
    return name === "Infinity" || name === "-Infinity" || name === "NaN";
}

/** @internal */
export function isCatchClauseVariableDeclaration(node: Node) {
    return node.kind === SyntaxKind.VariableDeclaration && node.parent.kind === SyntaxKind.CatchClause;
}

/** @internal */
export function isFunctionExpressionOrArrowFunction(node: Node): node is FunctionExpression | ArrowFunction {
    return node.kind === SyntaxKind.FunctionExpression || node.kind === SyntaxKind.ArrowFunction;
}

/** @internal */
export function escapeSnippetText(text: string): string {
    return text.replace(/\$/gm, () => "\\$");
}

/** @internal */
export function isNumericLiteralName(name: string | __String) {
    // The intent of numeric names is that
    //     - they are names with text in a numeric form, and that
    //     - setting properties/indexing with them is always equivalent to doing so with the numeric literal 'numLit',
    //         acquired by applying the abstract 'ToNumber' operation on the name's text.
    //
    // The subtlety is in the latter portion, as we cannot reliably say that anything that looks like a numeric literal is a numeric name.
    // In fact, it is the case that the text of the name must be equal to 'ToString(numLit)' for this to hold.
    //
    // Consider the property name '"0xF00D"'. When one indexes with '0xF00D', they are actually indexing with the value of 'ToString(0xF00D)'
    // according to the ECMAScript specification, so it is actually as if the user indexed with the string '"61453"'.
    // Thus, the text of all numeric literals equivalent to '61543' such as '0xF00D', '0xf00D', '0170015', etc. are not valid numeric names
    // because their 'ToString' representation is not equal to their original text.
    // This is motivated by ECMA-262 sections 9.3.1, 9.8.1, 11.1.5, and 11.2.1.
    //
    // Here, we test whether 'ToString(ToNumber(name))' is exactly equal to 'name'.
    // The '+' prefix operator is equivalent here to applying the abstract ToNumber operation.
    // Applying the 'toString()' method on a number gives us the abstract ToString operation on a number.
    //
    // Note that this accepts the values 'Infinity', '-Infinity', and 'NaN', and that this is intentional.
    // This is desired behavior, because when indexing with them as numeric entities, you are indexing
    // with the strings '"Infinity"', '"-Infinity"', and '"NaN"' respectively.
    return (+name).toString() === name;
}

/** @internal */
export function createPropertyNameNodeForIdentifierOrLiteral(name: string, target: ScriptTarget, singleQuote: boolean, stringNamed: boolean, isMethod: boolean) {
    const isMethodNamedNew = isMethod && name === "new";
    return !isMethodNamedNew && isIdentifierText(name, target) ? factory.createIdentifier(name) :
        !stringNamed && !isMethodNamedNew && isNumericLiteralName(name) && +name >= 0 ? factory.createNumericLiteral(+name) :
        factory.createStringLiteral(name, !!singleQuote);
}

/** @internal */
export function isThisTypeParameter(type: Type): boolean {
    return !!(type.flags & TypeFlags.TypeParameter && (type as TypeParameter).isThisType);
}

/** @internal */
export interface NodeModulePathParts {
    readonly topLevelNodeModulesIndex: number;
    readonly topLevelPackageNameIndex: number;
    readonly packageRootIndex: number;
    readonly fileNameIndex: number;
}
/** @internal */
export function getNodeModulePathParts(fullPath: string): NodeModulePathParts | undefined {
    // If fullPath can't be valid module file within node_modules, returns undefined.
    // Example of expected pattern: /base/path/node_modules/[@scope/otherpackage/@otherscope/node_modules/]package/[subdirectory/]file.js
    // Returns indices:                       ^            ^                                                      ^             ^

    let topLevelNodeModulesIndex = 0;
    let topLevelPackageNameIndex = 0;
    let packageRootIndex = 0;
    let fileNameIndex = 0;

    const enum States {
        BeforeNodeModules,
        NodeModules,
        Scope,
        PackageContent,
    }

    let partStart = 0;
    let partEnd = 0;
    let state = States.BeforeNodeModules;

    while (partEnd >= 0) {
        partStart = partEnd;
        partEnd = fullPath.indexOf("/", partStart + 1);
        switch (state) {
            case States.BeforeNodeModules:
                if (fullPath.indexOf(nodeModulesPathPart, partStart) === partStart) {
                    topLevelNodeModulesIndex = partStart;
                    topLevelPackageNameIndex = partEnd;
                    state = States.NodeModules;
                }
                break;
            case States.NodeModules:
            case States.Scope:
                if (state === States.NodeModules && fullPath.charAt(partStart + 1) === "@") {
                    state = States.Scope;
                }
                else {
                    packageRootIndex = partEnd;
                    state = States.PackageContent;
                }
                break;
            case States.PackageContent:
                if (fullPath.indexOf(nodeModulesPathPart, partStart) === partStart) {
                    state = States.NodeModules;
                }
                else {
                    state = States.PackageContent;
                }
                break;
        }
    }

    fileNameIndex = partStart;

    return state > States.NodeModules ? { topLevelNodeModulesIndex, topLevelPackageNameIndex, packageRootIndex, fileNameIndex } : undefined;
}

/** @internal */
export function getParameterTypeNode(parameter: ParameterDeclaration | JSDocParameterTag) {
    return parameter.kind === SyntaxKind.JSDocParameterTag ? parameter.typeExpression?.type : parameter.type;
}

/** @internal */
export function isTypeDeclaration(node: Node): node is TypeParameterDeclaration | ClassDeclaration | InterfaceDeclaration | TypeAliasDeclaration | JSDocTypedefTag | JSDocCallbackTag | JSDocEnumTag | EnumDeclaration | ImportClause | ImportSpecifier | ExportSpecifier {
    switch (node.kind) {
        case SyntaxKind.TypeParameter:
        case SyntaxKind.ClassDeclaration:
        case SyntaxKind.InterfaceDeclaration:
        case SyntaxKind.TypeAliasDeclaration:
        case SyntaxKind.EnumDeclaration:
        case SyntaxKind.JSDocTypedefTag:
        case SyntaxKind.JSDocCallbackTag:
        case SyntaxKind.JSDocEnumTag:
            return true;
        case SyntaxKind.ImportClause:
            return (node as ImportClause).isTypeOnly;
        case SyntaxKind.ImportSpecifier:
        case SyntaxKind.ExportSpecifier:
            return (node as ImportSpecifier | ExportSpecifier).parent.parent.isTypeOnly;
        default:
            return false;
    }
}

/** @internal */
export function canHaveExportModifier(node: Node): node is Extract<HasModifiers, Statement> {
    return isEnumDeclaration(node) || isVariableStatement(node) || isFunctionDeclaration(node) || isClassDeclaration(node)
        || isInterfaceDeclaration(node) || isTypeDeclaration(node) || (isModuleDeclaration(node) && !isExternalModuleAugmentation(node) && !isGlobalScopeAugmentation(node));
}

/** @internal */
export function isOptionalJSDocPropertyLikeTag(node: Node): node is JSDocPropertyLikeTag {
    if (!isJSDocPropertyLikeTag(node)) {
        return false;
    }
    const { isBracketed, typeExpression } = node;
    return isBracketed || !!typeExpression && typeExpression.type.kind === SyntaxKind.JSDocOptionalType;
}

/** @internal */
export function canUsePropertyAccess(name: string, languageVersion: ScriptTarget): boolean {
    if (name.length === 0) {
        return false;
    }
    const firstChar = name.charCodeAt(0);
    return firstChar === CharacterCodes.hash ?
        name.length > 1 && isIdentifierStart(name.charCodeAt(1), languageVersion) :
        isIdentifierStart(firstChar, languageVersion);
}

/** @internal */
export function hasTabstop(node: Node): boolean {
    return getSnippetElement(node)?.kind === SnippetKind.TabStop;
}

/** @internal */
export function isJSDocOptionalParameter(node: ParameterDeclaration) {
    return isInJSFile(node) && (
        // node.type should only be a JSDocOptionalType when node is a parameter of a JSDocFunctionType
        node.type && node.type.kind === SyntaxKind.JSDocOptionalType
        || getJSDocParameterTags(node).some(({ isBracketed, typeExpression }) => isBracketed || !!typeExpression && typeExpression.type.kind === SyntaxKind.JSDocOptionalType)
    );
}

/** @internal */
export function isOptionalDeclaration(declaration: Declaration): boolean {
    switch (declaration.kind) {
        case SyntaxKind.PropertyDeclaration:
        case SyntaxKind.PropertySignature:
            return !!(declaration as PropertyDeclaration | PropertySignature).questionToken;
        case SyntaxKind.Parameter:
            return !!(declaration as ParameterDeclaration).questionToken || isJSDocOptionalParameter(declaration as ParameterDeclaration);
        case SyntaxKind.JSDocPropertyTag:
        case SyntaxKind.JSDocParameterTag:
            return isOptionalJSDocPropertyLikeTag(declaration);
        default:
            return false;
    }
}

/** @internal */
export function isNonNullAccess(node: Node): node is AccessExpression {
    const kind = node.kind;
    return (kind === SyntaxKind.PropertyAccessExpression
        || kind === SyntaxKind.ElementAccessExpression) && isNonNullExpression((node as AccessExpression).expression);
}

/** @internal */
export function isJSDocSatisfiesExpression(node: Node): node is JSDocSatisfiesExpression {
    return isInJSFile(node) && isParenthesizedExpression(node) && hasJSDocNodes(node) && !!getJSDocSatisfiesTag(node);
}

/** @internal */
export function getJSDocSatisfiesExpressionType(node: JSDocSatisfiesExpression) {
    return Debug.checkDefined(tryGetJSDocSatisfiesTypeNode(node));
}

/** @internal */
export function tryGetJSDocSatisfiesTypeNode(node: Node) {
    const tag = getJSDocSatisfiesTag(node);
    return tag && tag.typeExpression && tag.typeExpression.type;
}

/** @internal */
export function getEscapedTextOfJsxAttributeName(node: JsxAttributeName): __String {
    return isIdentifier(node) ? node.escapedText : getEscapedTextOfJsxNamespacedName(node);
}

/** @internal */
export function getTextOfJsxAttributeName(node: JsxAttributeName): string {
    return isIdentifier(node) ? idText(node) : getTextOfJsxNamespacedName(node);
}

/** @internal */
export function isJsxAttributeName(node: Node): node is JsxAttributeName {
    const kind = node.kind;
    return kind === SyntaxKind.Identifier
        || kind === SyntaxKind.JsxNamespacedName;
}

/** @internal */
export function getEscapedTextOfJsxNamespacedName(node: JsxNamespacedName): __String {
    return `${node.namespace.escapedText}:${idText(node.name)}` as __String;
}

/** @internal */
export function getTextOfJsxNamespacedName(node: JsxNamespacedName) {
    return `${idText(node.namespace)}:${idText(node.name)}`;
}

/** @internal */
export function intrinsicTagNameToString(node: Identifier | JsxNamespacedName) {
    return isIdentifier(node) ? idText(node) : getTextOfJsxNamespacedName(node);
}

/**
 * Indicates whether a type can be used as a property name.
 * @internal
 */
export function isTypeUsableAsPropertyName(type: Type): type is StringLiteralType | NumberLiteralType | UniqueESSymbolType {
    return !!(type.flags & TypeFlags.StringOrNumberLiteralOrUnique);
}

/**
 * Gets the symbolic name for a member from its type.
 * @internal
 */
export function getPropertyNameFromType(type: StringLiteralType | NumberLiteralType | UniqueESSymbolType): __String {
    if (type.flags & TypeFlags.UniqueESSymbol) {
        return (type as UniqueESSymbolType).escapedName;
    }
    if (type.flags & (TypeFlags.StringLiteral | TypeFlags.NumberLiteral)) {
        return escapeLeadingUnderscores("" + (type as StringLiteralType | NumberLiteralType).value);
    }
    return Debug.fail();
}

/** @internal */
export function isExpandoPropertyDeclaration(declaration: Declaration | undefined): declaration is PropertyAccessExpression | ElementAccessExpression | BinaryExpression {
    return !!declaration && (isPropertyAccessExpression(declaration) || isElementAccessExpression(declaration) || isBinaryExpression(declaration));
}

/** @internal */
export function hasResolutionModeOverride(node: ImportTypeNode | ImportDeclaration | ExportDeclaration | undefined) {
    if (node === undefined) {
        return false;
    }
    return !!getResolutionModeOverride(node.attributes);
}

const stringReplace = String.prototype.replace;

/** @internal */
export function replaceFirstStar(s: string, replacement: string): string {
    // `s.replace("*", replacement)` triggers CodeQL as they think it's a potentially incorrect string escaping.
    // See: https://codeql.github.com/codeql-query-help/javascript/js-incomplete-sanitization/
    // But, we really do want to replace only the first star.
    // Attempt to defeat this analysis by indirectly calling the method.
    return stringReplace.call(s, "*", replacement);
}

/** @internal */
export function getNameFromImportAttribute(node: ImportAttribute) {
    return isIdentifier(node.name) ? node.name.escapedText : escapeLeadingUnderscores(node.name.text);
}


// --- ts_scanner.ts ---
import {
    append,
    arraysEqual,
    binarySearch,
    CharacterCodes,
    CommentDirective,
    CommentDirectiveType,
    CommentKind,
    CommentRange,
    compareValues,
    Debug,
    DiagnosticMessage,
    Diagnostics,
    identity,
    JSDocParsingMode,
    JSDocSyntaxKind,
    JsxTokenSyntaxKind,
    KeywordSyntaxKind,
    LanguageVariant,
    LineAndCharacter,
    MapLike,
    parsePseudoBigInt,
    positionIsSynthesized,
    PunctuationOrKeywordSyntaxKind,
    ScriptKind,
    ScriptTarget,
    SourceFileLike,
    SyntaxKind,
    TokenFlags,
} from "./_namespaces/ts";

export type ErrorCallback = (message: DiagnosticMessage, length: number, arg0?: any) => void;

/** @internal */
export function tokenIsIdentifierOrKeyword(token: SyntaxKind): boolean {
    return token >= SyntaxKind.Identifier;
}

/** @internal */
export function tokenIsIdentifierOrKeywordOrGreaterThan(token: SyntaxKind): boolean {
    return token === SyntaxKind.GreaterThanToken || tokenIsIdentifierOrKeyword(token);
}

export interface Scanner {
    /** @deprecated use {@link getTokenFullStart} */
    getStartPos(): number;
    getToken(): SyntaxKind;
    getTokenFullStart(): number;
    getTokenStart(): number;
    getTokenEnd(): number;
    /** @deprecated use {@link getTokenEnd} */
    getTextPos(): number;
    /** @deprecated use {@link getTokenStart} */
    getTokenPos(): number;
    getTokenText(): string;
    getTokenValue(): string;
    hasUnicodeEscape(): boolean;
    hasExtendedUnicodeEscape(): boolean;
    hasPrecedingLineBreak(): boolean;
    /** @internal */
    hasPrecedingJSDocComment(): boolean;
    isIdentifier(): boolean;
    isReservedWord(): boolean;
    isUnterminated(): boolean;
    /** @internal */
    getNumericLiteralFlags(): TokenFlags;
    /** @internal */
    getCommentDirectives(): CommentDirective[] | undefined;
    /** @internal */
    getTokenFlags(): TokenFlags;
    reScanGreaterToken(): SyntaxKind;
    reScanSlashToken(): SyntaxKind;
    reScanAsteriskEqualsToken(): SyntaxKind;
    reScanTemplateToken(isTaggedTemplate: boolean): SyntaxKind;
    /** @deprecated use {@link reScanTemplateToken}(false) */
    reScanTemplateHeadOrNoSubstitutionTemplate(): SyntaxKind;
    scanJsxIdentifier(): SyntaxKind;
    scanJsxAttributeValue(): SyntaxKind;
    reScanJsxAttributeValue(): SyntaxKind;
    reScanJsxToken(allowMultilineJsxText?: boolean): JsxTokenSyntaxKind;
    reScanLessThanToken(): SyntaxKind;
    reScanHashToken(): SyntaxKind;
    reScanQuestionToken(): SyntaxKind;
    reScanInvalidIdentifier(): SyntaxKind;
    scanJsxToken(): JsxTokenSyntaxKind;
    scanJsDocToken(): JSDocSyntaxKind;
    /** @internal */
    scanJSDocCommentTextToken(inBackticks: boolean): JSDocSyntaxKind | SyntaxKind.JSDocCommentTextToken;
    scan(): SyntaxKind;

    getText(): string;
    /** @internal */
    clearCommentDirectives(): void;
    // Sets the text for the scanner to scan.  An optional subrange starting point and length
    // can be provided to have the scanner only scan a portion of the text.
    setText(text: string | undefined, start?: number, length?: number): void;
    setOnError(onError: ErrorCallback | undefined): void;
    setScriptTarget(scriptTarget: ScriptTarget): void;
    setLanguageVariant(variant: LanguageVariant): void;
    setScriptKind(scriptKind: ScriptKind): void;
    setJSDocParsingMode(kind: JSDocParsingMode): void;
    /** @deprecated use {@link resetTokenState} */
    setTextPos(textPos: number): void;
    resetTokenState(pos: number): void;
    /** @internal */
    setInJSDocType(inType: boolean): void;
    // Invokes the provided callback then unconditionally restores the scanner to the state it
    // was in immediately prior to invoking the callback.  The result of invoking the callback
    // is returned from this function.
    lookAhead<T>(callback: () => T): T;

    // Invokes the callback with the scanner set to scan the specified range. When the callback
    // returns, the scanner is restored to the state it was in before scanRange was called.
    scanRange<T>(start: number, length: number, callback: () => T): T;

    // Invokes the provided callback.  If the callback returns something falsy, then it restores
    // the scanner to the state it was in immediately prior to invoking the callback.  If the
    // callback returns something truthy, then the scanner state is not rolled back.  The result
    // of invoking the callback is returned from this function.
    tryScan<T>(callback: () => T): T;
}

/** @internal */
export const textToKeywordObj: MapLike<KeywordSyntaxKind> = {
    abstract: SyntaxKind.AbstractKeyword,
    accessor: SyntaxKind.AccessorKeyword,
    any: SyntaxKind.AnyKeyword,
    as: SyntaxKind.AsKeyword,
    asserts: SyntaxKind.AssertsKeyword,
    assert: SyntaxKind.AssertKeyword,
    bigint: SyntaxKind.BigIntKeyword,
    boolean: SyntaxKind.BooleanKeyword,
    break: SyntaxKind.BreakKeyword,
    case: SyntaxKind.CaseKeyword,
    catch: SyntaxKind.CatchKeyword,
    class: SyntaxKind.ClassKeyword,
    continue: SyntaxKind.ContinueKeyword,
    const: SyntaxKind.ConstKeyword,
    ["" + "constructor"]: SyntaxKind.ConstructorKeyword,
    debugger: SyntaxKind.DebuggerKeyword,
    declare: SyntaxKind.DeclareKeyword,
    default: SyntaxKind.DefaultKeyword,
    delete: SyntaxKind.DeleteKeyword,
    do: SyntaxKind.DoKeyword,
    else: SyntaxKind.ElseKeyword,
    enum: SyntaxKind.EnumKeyword,
    export: SyntaxKind.ExportKeyword,
    extends: SyntaxKind.ExtendsKeyword,
    false: SyntaxKind.FalseKeyword,
    finally: SyntaxKind.FinallyKeyword,
    for: SyntaxKind.ForKeyword,
    from: SyntaxKind.FromKeyword,
    function: SyntaxKind.FunctionKeyword,
    get: SyntaxKind.GetKeyword,
    if: SyntaxKind.IfKeyword,
    implements: SyntaxKind.ImplementsKeyword,
    import: SyntaxKind.ImportKeyword,
    in: SyntaxKind.InKeyword,
    infer: SyntaxKind.InferKeyword,
    instanceof: SyntaxKind.InstanceOfKeyword,
    interface: SyntaxKind.InterfaceKeyword,
    intrinsic: SyntaxKind.IntrinsicKeyword,
    is: SyntaxKind.IsKeyword,
    keyof: SyntaxKind.KeyOfKeyword,
    let: SyntaxKind.LetKeyword,
    module: SyntaxKind.ModuleKeyword,
    namespace: SyntaxKind.NamespaceKeyword,
    never: SyntaxKind.NeverKeyword,
    new: SyntaxKind.NewKeyword,
    null: SyntaxKind.NullKeyword,
    number: SyntaxKind.NumberKeyword,
    object: SyntaxKind.ObjectKeyword,
    package: SyntaxKind.PackageKeyword,
    private: SyntaxKind.PrivateKeyword,
    protected: SyntaxKind.ProtectedKeyword,
    public: SyntaxKind.PublicKeyword,
    override: SyntaxKind.OverrideKeyword,
    out: SyntaxKind.OutKeyword,
    readonly: SyntaxKind.ReadonlyKeyword,
    require: SyntaxKind.RequireKeyword,
    global: SyntaxKind.GlobalKeyword,
    return: SyntaxKind.ReturnKeyword,
    satisfies: SyntaxKind.SatisfiesKeyword,
    set: SyntaxKind.SetKeyword,
    static: SyntaxKind.StaticKeyword,
    string: SyntaxKind.StringKeyword,
    super: SyntaxKind.SuperKeyword,
    switch: SyntaxKind.SwitchKeyword,
    symbol: SyntaxKind.SymbolKeyword,
    this: SyntaxKind.ThisKeyword,
    throw: SyntaxKind.ThrowKeyword,
    true: SyntaxKind.TrueKeyword,
    try: SyntaxKind.TryKeyword,
    type: SyntaxKind.TypeKeyword,
    typeof: SyntaxKind.TypeOfKeyword,
    undefined: SyntaxKind.UndefinedKeyword,
    unique: SyntaxKind.UniqueKeyword,
    unknown: SyntaxKind.UnknownKeyword,
    using: SyntaxKind.UsingKeyword,
    var: SyntaxKind.VarKeyword,
    void: SyntaxKind.VoidKeyword,
    while: SyntaxKind.WhileKeyword,
    with: SyntaxKind.WithKeyword,
    yield: SyntaxKind.YieldKeyword,
    async: SyntaxKind.AsyncKeyword,
    await: SyntaxKind.AwaitKeyword,
    of: SyntaxKind.OfKeyword,
};

const textToKeyword = new Map(Object.entries(textToKeywordObj));

const textToToken = new Map(Object.entries({
    ...textToKeywordObj,
    "{": SyntaxKind.OpenBraceToken,
    "}": SyntaxKind.CloseBraceToken,
    "(": SyntaxKind.OpenParenToken,
    ")": SyntaxKind.CloseParenToken,
    "[": SyntaxKind.OpenBracketToken,
    "]": SyntaxKind.CloseBracketToken,
    ".": SyntaxKind.DotToken,
    "...": SyntaxKind.DotDotDotToken,
    ";": SyntaxKind.SemicolonToken,
    ",": SyntaxKind.CommaToken,
    "<": SyntaxKind.LessThanToken,
    ">": SyntaxKind.GreaterThanToken,
    "<=": SyntaxKind.LessThanEqualsToken,
    ">=": SyntaxKind.GreaterThanEqualsToken,
    "==": SyntaxKind.EqualsEqualsToken,
    "!=": SyntaxKind.ExclamationEqualsToken,
    "===": SyntaxKind.EqualsEqualsEqualsToken,
    "!==": SyntaxKind.ExclamationEqualsEqualsToken,
    "=>": SyntaxKind.EqualsGreaterThanToken,
    "+": SyntaxKind.PlusToken,
    "-": SyntaxKind.MinusToken,
    "**": SyntaxKind.AsteriskAsteriskToken,
    "*": SyntaxKind.AsteriskToken,
    "/": SyntaxKind.SlashToken,
    "%": SyntaxKind.PercentToken,
    "++": SyntaxKind.PlusPlusToken,
    "--": SyntaxKind.MinusMinusToken,
    "<<": SyntaxKind.LessThanLessThanToken,
    "</": SyntaxKind.LessThanSlashToken,
    ">>": SyntaxKind.GreaterThanGreaterThanToken,
    ">>>": SyntaxKind.GreaterThanGreaterThanGreaterThanToken,
    "&": SyntaxKind.AmpersandToken,
    "|": SyntaxKind.BarToken,
    "^": SyntaxKind.CaretToken,
    "!": SyntaxKind.ExclamationToken,
    "~": SyntaxKind.TildeToken,
    "&&": SyntaxKind.AmpersandAmpersandToken,
    "||": SyntaxKind.BarBarToken,
    "?": SyntaxKind.QuestionToken,
    "??": SyntaxKind.QuestionQuestionToken,
    "?.": SyntaxKind.QuestionDotToken,
    ":": SyntaxKind.ColonToken,
    "=": SyntaxKind.EqualsToken,
    "+=": SyntaxKind.PlusEqualsToken,
    "-=": SyntaxKind.MinusEqualsToken,
    "*=": SyntaxKind.AsteriskEqualsToken,
    "**=": SyntaxKind.AsteriskAsteriskEqualsToken,
    "/=": SyntaxKind.SlashEqualsToken,
    "%=": SyntaxKind.PercentEqualsToken,
    "<<=": SyntaxKind.LessThanLessThanEqualsToken,
    ">>=": SyntaxKind.GreaterThanGreaterThanEqualsToken,
    ">>>=": SyntaxKind.GreaterThanGreaterThanGreaterThanEqualsToken,
    "&=": SyntaxKind.AmpersandEqualsToken,
    "|=": SyntaxKind.BarEqualsToken,
    "^=": SyntaxKind.CaretEqualsToken,
    "||=": SyntaxKind.BarBarEqualsToken,
    "&&=": SyntaxKind.AmpersandAmpersandEqualsToken,
    "??=": SyntaxKind.QuestionQuestionEqualsToken,
    "@": SyntaxKind.AtToken,
    "#": SyntaxKind.HashToken,
    "`": SyntaxKind.BacktickToken,
}));

/*
    As per ECMAScript Language Specification 3th Edition, Section 7.6: Identifiers
    IdentifierStart ::
        Can contain Unicode 3.0.0 categories:
        Uppercase letter (Lu),
        Lowercase letter (Ll),
        Titlecase letter (Lt),
        Modifier letter (Lm),
        Other letter (Lo), or
        Letter number (Nl).
    IdentifierPart :: =
        Can contain IdentifierStart + Unicode 3.0.0 categories:
        Non-spacing mark (Mn),
        Combining spacing mark (Mc),
        Decimal number (Nd), or
        Connector punctuation (Pc).

    Codepoint ranges for ES3 Identifiers are extracted from the Unicode 3.0.0 specification at:
    http://www.unicode.org/Public/3.0-Update/UnicodeData-3.0.0.txt
*/
// dprint-ignore
const unicodeES3IdentifierStart = [170, 170, 181, 181, 186, 186, 192, 214, 216, 246, 248, 543, 546, 563, 592, 685, 688, 696, 699, 705, 720, 721, 736, 740, 750, 750, 890, 890, 902, 902, 904, 906, 908, 908, 910, 929, 931, 974, 976, 983, 986, 1011, 1024, 1153, 1164, 1220, 1223, 1224, 1227, 1228, 1232, 1269, 1272, 1273, 1329, 1366, 1369, 1369, 1377, 1415, 1488, 1514, 1520, 1522, 1569, 1594, 1600, 1610, 1649, 1747, 1749, 1749, 1765, 1766, 1786, 1788, 1808, 1808, 1810, 1836, 1920, 1957, 2309, 2361, 2365, 2365, 2384, 2384, 2392, 2401, 2437, 2444, 2447, 2448, 2451, 2472, 2474, 2480, 2482, 2482, 2486, 2489, 2524, 2525, 2527, 2529, 2544, 2545, 2565, 2570, 2575, 2576, 2579, 2600, 2602, 2608, 2610, 2611, 2613, 2614, 2616, 2617, 2649, 2652, 2654, 2654, 2674, 2676, 2693, 2699, 2701, 2701, 2703, 2705, 2707, 2728, 2730, 2736, 2738, 2739, 2741, 2745, 2749, 2749, 2768, 2768, 2784, 2784, 2821, 2828, 2831, 2832, 2835, 2856, 2858, 2864, 2866, 2867, 2870, 2873, 2877, 2877, 2908, 2909, 2911, 2913, 2949, 2954, 2958, 2960, 2962, 2965, 2969, 2970, 2972, 2972, 2974, 2975, 2979, 2980, 2984, 2986, 2990, 2997, 2999, 3001, 3077, 3084, 3086, 3088, 3090, 3112, 3114, 3123, 3125, 3129, 3168, 3169, 3205, 3212, 3214, 3216, 3218, 3240, 3242, 3251, 3253, 3257, 3294, 3294, 3296, 3297, 3333, 3340, 3342, 3344, 3346, 3368, 3370, 3385, 3424, 3425, 3461, 3478, 3482, 3505, 3507, 3515, 3517, 3517, 3520, 3526, 3585, 3632, 3634, 3635, 3648, 3654, 3713, 3714, 3716, 3716, 3719, 3720, 3722, 3722, 3725, 3725, 3732, 3735, 3737, 3743, 3745, 3747, 3749, 3749, 3751, 3751, 3754, 3755, 3757, 3760, 3762, 3763, 3773, 3773, 3776, 3780, 3782, 3782, 3804, 3805, 3840, 3840, 3904, 3911, 3913, 3946, 3976, 3979, 4096, 4129, 4131, 4135, 4137, 4138, 4176, 4181, 4256, 4293, 4304, 4342, 4352, 4441, 4447, 4514, 4520, 4601, 4608, 4614, 4616, 4678, 4680, 4680, 4682, 4685, 4688, 4694, 4696, 4696, 4698, 4701, 4704, 4742, 4744, 4744, 4746, 4749, 4752, 4782, 4784, 4784, 4786, 4789, 4792, 4798, 4800, 4800, 4802, 4805, 4808, 4814, 4816, 4822, 4824, 4846, 4848, 4878, 4880, 4880, 4882, 4885, 4888, 4894, 4896, 4934, 4936, 4954, 5024, 5108, 5121, 5740, 5743, 5750, 5761, 5786, 5792, 5866, 6016, 6067, 6176, 6263, 6272, 6312, 7680, 7835, 7840, 7929, 7936, 7957, 7960, 7965, 7968, 8005, 8008, 8013, 8016, 8023, 8025, 8025, 8027, 8027, 8029, 8029, 8031, 8061, 8064, 8116, 8118, 8124, 8126, 8126, 8130, 8132, 8134, 8140, 8144, 8147, 8150, 8155, 8160, 8172, 8178, 8180, 8182, 8188, 8319, 8319, 8450, 8450, 8455, 8455, 8458, 8467, 8469, 8469, 8473, 8477, 8484, 8484, 8486, 8486, 8488, 8488, 8490, 8493, 8495, 8497, 8499, 8505, 8544, 8579, 12293, 12295, 12321, 12329, 12337, 12341, 12344, 12346, 12353, 12436, 12445, 12446, 12449, 12538, 12540, 12542, 12549, 12588, 12593, 12686, 12704, 12727, 13312, 19893, 19968, 40869, 40960, 42124, 44032, 55203, 63744, 64045, 64256, 64262, 64275, 64279, 64285, 64285, 64287, 64296, 64298, 64310, 64312, 64316, 64318, 64318, 64320, 64321, 64323, 64324, 64326, 64433, 64467, 64829, 64848, 64911, 64914, 64967, 65008, 65019, 65136, 65138, 65140, 65140, 65142, 65276, 65313, 65338, 65345, 65370, 65382, 65470, 65474, 65479, 65482, 65487, 65490, 65495, 65498, 65500 ];
// dprint-ignore
const unicodeES3IdentifierPart = [170, 170, 181, 181, 186, 186, 192, 214, 216, 246, 248, 543, 546, 563, 592, 685, 688, 696, 699, 705, 720, 721, 736, 740, 750, 750, 768, 846, 864, 866, 890, 890, 902, 902, 904, 906, 908, 908, 910, 929, 931, 974, 976, 983, 986, 1011, 1024, 1153, 1155, 1158, 1164, 1220, 1223, 1224, 1227, 1228, 1232, 1269, 1272, 1273, 1329, 1366, 1369, 1369, 1377, 1415, 1425, 1441, 1443, 1465, 1467, 1469, 1471, 1471, 1473, 1474, 1476, 1476, 1488, 1514, 1520, 1522, 1569, 1594, 1600, 1621, 1632, 1641, 1648, 1747, 1749, 1756, 1759, 1768, 1770, 1773, 1776, 1788, 1808, 1836, 1840, 1866, 1920, 1968, 2305, 2307, 2309, 2361, 2364, 2381, 2384, 2388, 2392, 2403, 2406, 2415, 2433, 2435, 2437, 2444, 2447, 2448, 2451, 2472, 2474, 2480, 2482, 2482, 2486, 2489, 2492, 2492, 2494, 2500, 2503, 2504, 2507, 2509, 2519, 2519, 2524, 2525, 2527, 2531, 2534, 2545, 2562, 2562, 2565, 2570, 2575, 2576, 2579, 2600, 2602, 2608, 2610, 2611, 2613, 2614, 2616, 2617, 2620, 2620, 2622, 2626, 2631, 2632, 2635, 2637, 2649, 2652, 2654, 2654, 2662, 2676, 2689, 2691, 2693, 2699, 2701, 2701, 2703, 2705, 2707, 2728, 2730, 2736, 2738, 2739, 2741, 2745, 2748, 2757, 2759, 2761, 2763, 2765, 2768, 2768, 2784, 2784, 2790, 2799, 2817, 2819, 2821, 2828, 2831, 2832, 2835, 2856, 2858, 2864, 2866, 2867, 2870, 2873, 2876, 2883, 2887, 2888, 2891, 2893, 2902, 2903, 2908, 2909, 2911, 2913, 2918, 2927, 2946, 2947, 2949, 2954, 2958, 2960, 2962, 2965, 2969, 2970, 2972, 2972, 2974, 2975, 2979, 2980, 2984, 2986, 2990, 2997, 2999, 3001, 3006, 3010, 3014, 3016, 3018, 3021, 3031, 3031, 3047, 3055, 3073, 3075, 3077, 3084, 3086, 3088, 3090, 3112, 3114, 3123, 3125, 3129, 3134, 3140, 3142, 3144, 3146, 3149, 3157, 3158, 3168, 3169, 3174, 3183, 3202, 3203, 3205, 3212, 3214, 3216, 3218, 3240, 3242, 3251, 3253, 3257, 3262, 3268, 3270, 3272, 3274, 3277, 3285, 3286, 3294, 3294, 3296, 3297, 3302, 3311, 3330, 3331, 3333, 3340, 3342, 3344, 3346, 3368, 3370, 3385, 3390, 3395, 3398, 3400, 3402, 3405, 3415, 3415, 3424, 3425, 3430, 3439, 3458, 3459, 3461, 3478, 3482, 3505, 3507, 3515, 3517, 3517, 3520, 3526, 3530, 3530, 3535, 3540, 3542, 3542, 3544, 3551, 3570, 3571, 3585, 3642, 3648, 3662, 3664, 3673, 3713, 3714, 3716, 3716, 3719, 3720, 3722, 3722, 3725, 3725, 3732, 3735, 3737, 3743, 3745, 3747, 3749, 3749, 3751, 3751, 3754, 3755, 3757, 3769, 3771, 3773, 3776, 3780, 3782, 3782, 3784, 3789, 3792, 3801, 3804, 3805, 3840, 3840, 3864, 3865, 3872, 3881, 3893, 3893, 3895, 3895, 3897, 3897, 3902, 3911, 3913, 3946, 3953, 3972, 3974, 3979, 3984, 3991, 3993, 4028, 4038, 4038, 4096, 4129, 4131, 4135, 4137, 4138, 4140, 4146, 4150, 4153, 4160, 4169, 4176, 4185, 4256, 4293, 4304, 4342, 4352, 4441, 4447, 4514, 4520, 4601, 4608, 4614, 4616, 4678, 4680, 4680, 4682, 4685, 4688, 4694, 4696, 4696, 4698, 4701, 4704, 4742, 4744, 4744, 4746, 4749, 4752, 4782, 4784, 4784, 4786, 4789, 4792, 4798, 4800, 4800, 4802, 4805, 4808, 4814, 4816, 4822, 4824, 4846, 4848, 4878, 4880, 4880, 4882, 4885, 4888, 4894, 4896, 4934, 4936, 4954, 4969, 4977, 5024, 5108, 5121, 5740, 5743, 5750, 5761, 5786, 5792, 5866, 6016, 6099, 6112, 6121, 6160, 6169, 6176, 6263, 6272, 6313, 7680, 7835, 7840, 7929, 7936, 7957, 7960, 7965, 7968, 8005, 8008, 8013, 8016, 8023, 8025, 8025, 8027, 8027, 8029, 8029, 8031, 8061, 8064, 8116, 8118, 8124, 8126, 8126, 8130, 8132, 8134, 8140, 8144, 8147, 8150, 8155, 8160, 8172, 8178, 8180, 8182, 8188, 8255, 8256, 8319, 8319, 8400, 8412, 8417, 8417, 8450, 8450, 8455, 8455, 8458, 8467, 8469, 8469, 8473, 8477, 8484, 8484, 8486, 8486, 8488, 8488, 8490, 8493, 8495, 8497, 8499, 8505, 8544, 8579, 12293, 12295, 12321, 12335, 12337, 12341, 12344, 12346, 12353, 12436, 12441, 12442, 12445, 12446, 12449, 12542, 12549, 12588, 12593, 12686, 12704, 12727, 13312, 19893, 19968, 40869, 40960, 42124, 44032, 55203, 63744, 64045, 64256, 64262, 64275, 64279, 64285, 64296, 64298, 64310, 64312, 64316, 64318, 64318, 64320, 64321, 64323, 64324, 64326, 64433, 64467, 64829, 64848, 64911, 64914, 64967, 65008, 65019, 65056, 65059, 65075, 65076, 65101, 65103, 65136, 65138, 65140, 65140, 65142, 65276, 65296, 65305, 65313, 65338, 65343, 65343, 65345, 65370, 65381, 65470, 65474, 65479, 65482, 65487, 65490, 65495, 65498, 65500 ];

/*
    As per ECMAScript Language Specification 5th Edition, Section 7.6: ISyntaxToken Names and Identifiers
    IdentifierStart ::
        Can contain Unicode 6.2 categories:
        Uppercase letter (Lu),
        Lowercase letter (Ll),
        Titlecase letter (Lt),
        Modifier letter (Lm),
        Other letter (Lo), or
        Letter number (Nl).
    IdentifierPart ::
        Can contain IdentifierStart + Unicode 6.2 categories:
        Non-spacing mark (Mn),
        Combining spacing mark (Mc),
        Decimal number (Nd),
        Connector punctuation (Pc),
        <ZWNJ>, or
        <ZWJ>.

    Codepoint ranges for ES5 Identifiers are extracted from the Unicode 6.2 specification at:
    http://www.unicode.org/Public/6.2.0/ucd/UnicodeData.txt
*/
// dprint-ignore
const unicodeES5IdentifierStart = [170, 170, 181, 181, 186, 186, 192, 214, 216, 246, 248, 705, 710, 721, 736, 740, 748, 748, 750, 750, 880, 884, 886, 887, 890, 893, 902, 902, 904, 906, 908, 908, 910, 929, 931, 1013, 1015, 1153, 1162, 1319, 1329, 1366, 1369, 1369, 1377, 1415, 1488, 1514, 1520, 1522, 1568, 1610, 1646, 1647, 1649, 1747, 1749, 1749, 1765, 1766, 1774, 1775, 1786, 1788, 1791, 1791, 1808, 1808, 1810, 1839, 1869, 1957, 1969, 1969, 1994, 2026, 2036, 2037, 2042, 2042, 2048, 2069, 2074, 2074, 2084, 2084, 2088, 2088, 2112, 2136, 2208, 2208, 2210, 2220, 2308, 2361, 2365, 2365, 2384, 2384, 2392, 2401, 2417, 2423, 2425, 2431, 2437, 2444, 2447, 2448, 2451, 2472, 2474, 2480, 2482, 2482, 2486, 2489, 2493, 2493, 2510, 2510, 2524, 2525, 2527, 2529, 2544, 2545, 2565, 2570, 2575, 2576, 2579, 2600, 2602, 2608, 2610, 2611, 2613, 2614, 2616, 2617, 2649, 2652, 2654, 2654, 2674, 2676, 2693, 2701, 2703, 2705, 2707, 2728, 2730, 2736, 2738, 2739, 2741, 2745, 2749, 2749, 2768, 2768, 2784, 2785, 2821, 2828, 2831, 2832, 2835, 2856, 2858, 2864, 2866, 2867, 2869, 2873, 2877, 2877, 2908, 2909, 2911, 2913, 2929, 2929, 2947, 2947, 2949, 2954, 2958, 2960, 2962, 2965, 2969, 2970, 2972, 2972, 2974, 2975, 2979, 2980, 2984, 2986, 2990, 3001, 3024, 3024, 3077, 3084, 3086, 3088, 3090, 3112, 3114, 3123, 3125, 3129, 3133, 3133, 3160, 3161, 3168, 3169, 3205, 3212, 3214, 3216, 3218, 3240, 3242, 3251, 3253, 3257, 3261, 3261, 3294, 3294, 3296, 3297, 3313, 3314, 3333, 3340, 3342, 3344, 3346, 3386, 3389, 3389, 3406, 3406, 3424, 3425, 3450, 3455, 3461, 3478, 3482, 3505, 3507, 3515, 3517, 3517, 3520, 3526, 3585, 3632, 3634, 3635, 3648, 3654, 3713, 3714, 3716, 3716, 3719, 3720, 3722, 3722, 3725, 3725, 3732, 3735, 3737, 3743, 3745, 3747, 3749, 3749, 3751, 3751, 3754, 3755, 3757, 3760, 3762, 3763, 3773, 3773, 3776, 3780, 3782, 3782, 3804, 3807, 3840, 3840, 3904, 3911, 3913, 3948, 3976, 3980, 4096, 4138, 4159, 4159, 4176, 4181, 4186, 4189, 4193, 4193, 4197, 4198, 4206, 4208, 4213, 4225, 4238, 4238, 4256, 4293, 4295, 4295, 4301, 4301, 4304, 4346, 4348, 4680, 4682, 4685, 4688, 4694, 4696, 4696, 4698, 4701, 4704, 4744, 4746, 4749, 4752, 4784, 4786, 4789, 4792, 4798, 4800, 4800, 4802, 4805, 4808, 4822, 4824, 4880, 4882, 4885, 4888, 4954, 4992, 5007, 5024, 5108, 5121, 5740, 5743, 5759, 5761, 5786, 5792, 5866, 5870, 5872, 5888, 5900, 5902, 5905, 5920, 5937, 5952, 5969, 5984, 5996, 5998, 6000, 6016, 6067, 6103, 6103, 6108, 6108, 6176, 6263, 6272, 6312, 6314, 6314, 6320, 6389, 6400, 6428, 6480, 6509, 6512, 6516, 6528, 6571, 6593, 6599, 6656, 6678, 6688, 6740, 6823, 6823, 6917, 6963, 6981, 6987, 7043, 7072, 7086, 7087, 7098, 7141, 7168, 7203, 7245, 7247, 7258, 7293, 7401, 7404, 7406, 7409, 7413, 7414, 7424, 7615, 7680, 7957, 7960, 7965, 7968, 8005, 8008, 8013, 8016, 8023, 8025, 8025, 8027, 8027, 8029, 8029, 8031, 8061, 8064, 8116, 8118, 8124, 8126, 8126, 8130, 8132, 8134, 8140, 8144, 8147, 8150, 8155, 8160, 8172, 8178, 8180, 8182, 8188, 8305, 8305, 8319, 8319, 8336, 8348, 8450, 8450, 8455, 8455, 8458, 8467, 8469, 8469, 8473, 8477, 8484, 8484, 8486, 8486, 8488, 8488, 8490, 8493, 8495, 8505, 8508, 8511, 8517, 8521, 8526, 8526, 8544, 8584, 11264, 11310, 11312, 11358, 11360, 11492, 11499, 11502, 11506, 11507, 11520, 11557, 11559, 11559, 11565, 11565, 11568, 11623, 11631, 11631, 11648, 11670, 11680, 11686, 11688, 11694, 11696, 11702, 11704, 11710, 11712, 11718, 11720, 11726, 11728, 11734, 11736, 11742, 11823, 11823, 12293, 12295, 12321, 12329, 12337, 12341, 12344, 12348, 12353, 12438, 12445, 12447, 12449, 12538, 12540, 12543, 12549, 12589, 12593, 12686, 12704, 12730, 12784, 12799, 13312, 19893, 19968, 40908, 40960, 42124, 42192, 42237, 42240, 42508, 42512, 42527, 42538, 42539, 42560, 42606, 42623, 42647, 42656, 42735, 42775, 42783, 42786, 42888, 42891, 42894, 42896, 42899, 42912, 42922, 43000, 43009, 43011, 43013, 43015, 43018, 43020, 43042, 43072, 43123, 43138, 43187, 43250, 43255, 43259, 43259, 43274, 43301, 43312, 43334, 43360, 43388, 43396, 43442, 43471, 43471, 43520, 43560, 43584, 43586, 43588, 43595, 43616, 43638, 43642, 43642, 43648, 43695, 43697, 43697, 43701, 43702, 43705, 43709, 43712, 43712, 43714, 43714, 43739, 43741, 43744, 43754, 43762, 43764, 43777, 43782, 43785, 43790, 43793, 43798, 43808, 43814, 43816, 43822, 43968, 44002, 44032, 55203, 55216, 55238, 55243, 55291, 63744, 64109, 64112, 64217, 64256, 64262, 64275, 64279, 64285, 64285, 64287, 64296, 64298, 64310, 64312, 64316, 64318, 64318, 64320, 64321, 64323, 64324, 64326, 64433, 64467, 64829, 64848, 64911, 64914, 64967, 65008, 65019, 65136, 65140, 65142, 65276, 65313, 65338, 65345, 65370, 65382, 65470, 65474, 65479, 65482, 65487, 65490, 65495, 65498, 65500 ];
// dprint-ignore
const unicodeES5IdentifierPart = [170, 170, 181, 181, 186, 186, 192, 214, 216, 246, 248, 705, 710, 721, 736, 740, 748, 748, 750, 750, 768, 884, 886, 887, 890, 893, 902, 902, 904, 906, 908, 908, 910, 929, 931, 1013, 1015, 1153, 1155, 1159, 1162, 1319, 1329, 1366, 1369, 1369, 1377, 1415, 1425, 1469, 1471, 1471, 1473, 1474, 1476, 1477, 1479, 1479, 1488, 1514, 1520, 1522, 1552, 1562, 1568, 1641, 1646, 1747, 1749, 1756, 1759, 1768, 1770, 1788, 1791, 1791, 1808, 1866, 1869, 1969, 1984, 2037, 2042, 2042, 2048, 2093, 2112, 2139, 2208, 2208, 2210, 2220, 2276, 2302, 2304, 2403, 2406, 2415, 2417, 2423, 2425, 2431, 2433, 2435, 2437, 2444, 2447, 2448, 2451, 2472, 2474, 2480, 2482, 2482, 2486, 2489, 2492, 2500, 2503, 2504, 2507, 2510, 2519, 2519, 2524, 2525, 2527, 2531, 2534, 2545, 2561, 2563, 2565, 2570, 2575, 2576, 2579, 2600, 2602, 2608, 2610, 2611, 2613, 2614, 2616, 2617, 2620, 2620, 2622, 2626, 2631, 2632, 2635, 2637, 2641, 2641, 2649, 2652, 2654, 2654, 2662, 2677, 2689, 2691, 2693, 2701, 2703, 2705, 2707, 2728, 2730, 2736, 2738, 2739, 2741, 2745, 2748, 2757, 2759, 2761, 2763, 2765, 2768, 2768, 2784, 2787, 2790, 2799, 2817, 2819, 2821, 2828, 2831, 2832, 2835, 2856, 2858, 2864, 2866, 2867, 2869, 2873, 2876, 2884, 2887, 2888, 2891, 2893, 2902, 2903, 2908, 2909, 2911, 2915, 2918, 2927, 2929, 2929, 2946, 2947, 2949, 2954, 2958, 2960, 2962, 2965, 2969, 2970, 2972, 2972, 2974, 2975, 2979, 2980, 2984, 2986, 2990, 3001, 3006, 3010, 3014, 3016, 3018, 3021, 3024, 3024, 3031, 3031, 3046, 3055, 3073, 3075, 3077, 3084, 3086, 3088, 3090, 3112, 3114, 3123, 3125, 3129, 3133, 3140, 3142, 3144, 3146, 3149, 3157, 3158, 3160, 3161, 3168, 3171, 3174, 3183, 3202, 3203, 3205, 3212, 3214, 3216, 3218, 3240, 3242, 3251, 3253, 3257, 3260, 3268, 3270, 3272, 3274, 3277, 3285, 3286, 3294, 3294, 3296, 3299, 3302, 3311, 3313, 3314, 3330, 3331, 3333, 3340, 3342, 3344, 3346, 3386, 3389, 3396, 3398, 3400, 3402, 3406, 3415, 3415, 3424, 3427, 3430, 3439, 3450, 3455, 3458, 3459, 3461, 3478, 3482, 3505, 3507, 3515, 3517, 3517, 3520, 3526, 3530, 3530, 3535, 3540, 3542, 3542, 3544, 3551, 3570, 3571, 3585, 3642, 3648, 3662, 3664, 3673, 3713, 3714, 3716, 3716, 3719, 3720, 3722, 3722, 3725, 3725, 3732, 3735, 3737, 3743, 3745, 3747, 3749, 3749, 3751, 3751, 3754, 3755, 3757, 3769, 3771, 3773, 3776, 3780, 3782, 3782, 3784, 3789, 3792, 3801, 3804, 3807, 3840, 3840, 3864, 3865, 3872, 3881, 3893, 3893, 3895, 3895, 3897, 3897, 3902, 3911, 3913, 3948, 3953, 3972, 3974, 3991, 3993, 4028, 4038, 4038, 4096, 4169, 4176, 4253, 4256, 4293, 4295, 4295, 4301, 4301, 4304, 4346, 4348, 4680, 4682, 4685, 4688, 4694, 4696, 4696, 4698, 4701, 4704, 4744, 4746, 4749, 4752, 4784, 4786, 4789, 4792, 4798, 4800, 4800, 4802, 4805, 4808, 4822, 4824, 4880, 4882, 4885, 4888, 4954, 4957, 4959, 4992, 5007, 5024, 5108, 5121, 5740, 5743, 5759, 5761, 5786, 5792, 5866, 5870, 5872, 5888, 5900, 5902, 5908, 5920, 5940, 5952, 5971, 5984, 5996, 5998, 6000, 6002, 6003, 6016, 6099, 6103, 6103, 6108, 6109, 6112, 6121, 6155, 6157, 6160, 6169, 6176, 6263, 6272, 6314, 6320, 6389, 6400, 6428, 6432, 6443, 6448, 6459, 6470, 6509, 6512, 6516, 6528, 6571, 6576, 6601, 6608, 6617, 6656, 6683, 6688, 6750, 6752, 6780, 6783, 6793, 6800, 6809, 6823, 6823, 6912, 6987, 6992, 7001, 7019, 7027, 7040, 7155, 7168, 7223, 7232, 7241, 7245, 7293, 7376, 7378, 7380, 7414, 7424, 7654, 7676, 7957, 7960, 7965, 7968, 8005, 8008, 8013, 8016, 8023, 8025, 8025, 8027, 8027, 8029, 8029, 8031, 8061, 8064, 8116, 8118, 8124, 8126, 8126, 8130, 8132, 8134, 8140, 8144, 8147, 8150, 8155, 8160, 8172, 8178, 8180, 8182, 8188, 8204, 8205, 8255, 8256, 8276, 8276, 8305, 8305, 8319, 8319, 8336, 8348, 8400, 8412, 8417, 8417, 8421, 8432, 8450, 8450, 8455, 8455, 8458, 8467, 8469, 8469, 8473, 8477, 8484, 8484, 8486, 8486, 8488, 8488, 8490, 8493, 8495, 8505, 8508, 8511, 8517, 8521, 8526, 8526, 8544, 8584, 11264, 11310, 11312, 11358, 11360, 11492, 11499, 11507, 11520, 11557, 11559, 11559, 11565, 11565, 11568, 11623, 11631, 11631, 11647, 11670, 11680, 11686, 11688, 11694, 11696, 11702, 11704, 11710, 11712, 11718, 11720, 11726, 11728, 11734, 11736, 11742, 11744, 11775, 11823, 11823, 12293, 12295, 12321, 12335, 12337, 12341, 12344, 12348, 12353, 12438, 12441, 12442, 12445, 12447, 12449, 12538, 12540, 12543, 12549, 12589, 12593, 12686, 12704, 12730, 12784, 12799, 13312, 19893, 19968, 40908, 40960, 42124, 42192, 42237, 42240, 42508, 42512, 42539, 42560, 42607, 42612, 42621, 42623, 42647, 42655, 42737, 42775, 42783, 42786, 42888, 42891, 42894, 42896, 42899, 42912, 42922, 43000, 43047, 43072, 43123, 43136, 43204, 43216, 43225, 43232, 43255, 43259, 43259, 43264, 43309, 43312, 43347, 43360, 43388, 43392, 43456, 43471, 43481, 43520, 43574, 43584, 43597, 43600, 43609, 43616, 43638, 43642, 43643, 43648, 43714, 43739, 43741, 43744, 43759, 43762, 43766, 43777, 43782, 43785, 43790, 43793, 43798, 43808, 43814, 43816, 43822, 43968, 44010, 44012, 44013, 44016, 44025, 44032, 55203, 55216, 55238, 55243, 55291, 63744, 64109, 64112, 64217, 64256, 64262, 64275, 64279, 64285, 64296, 64298, 64310, 64312, 64316, 64318, 64318, 64320, 64321, 64323, 64324, 64326, 64433, 64467, 64829, 64848, 64911, 64914, 64967, 65008, 65019, 65024, 65039, 65056, 65062, 65075, 65076, 65101, 65103, 65136, 65140, 65142, 65276, 65296, 65305, 65313, 65338, 65343, 65343, 65345, 65370, 65382, 65470, 65474, 65479, 65482, 65487, 65490, 65495, 65498, 65500 ];

/**
 * Generated by scripts/regenerate-unicode-identifier-parts.js on node v12.4.0 with unicode 12.1
 * based on http://www.unicode.org/reports/tr31/ and https://www.ecma-international.org/ecma-262/6.0/#sec-names-and-keywords
 * unicodeESNextIdentifierStart corresponds to the ID_Start and Other_ID_Start property, and
 * unicodeESNextIdentifierPart corresponds to ID_Continue, Other_ID_Continue, plus ID_Start and Other_ID_Start
 */
// dprint-ignore
const unicodeESNextIdentifierStart = [65, 90, 97, 122, 170, 170, 181, 181, 186, 186, 192, 214, 216, 246, 248, 705, 710, 721, 736, 740, 748, 748, 750, 750, 880, 884, 886, 887, 890, 893, 895, 895, 902, 902, 904, 906, 908, 908, 910, 929, 931, 1013, 1015, 1153, 1162, 1327, 1329, 1366, 1369, 1369, 1376, 1416, 1488, 1514, 1519, 1522, 1568, 1610, 1646, 1647, 1649, 1747, 1749, 1749, 1765, 1766, 1774, 1775, 1786, 1788, 1791, 1791, 1808, 1808, 1810, 1839, 1869, 1957, 1969, 1969, 1994, 2026, 2036, 2037, 2042, 2042, 2048, 2069, 2074, 2074, 2084, 2084, 2088, 2088, 2112, 2136, 2144, 2154, 2208, 2228, 2230, 2237, 2308, 2361, 2365, 2365, 2384, 2384, 2392, 2401, 2417, 2432, 2437, 2444, 2447, 2448, 2451, 2472, 2474, 2480, 2482, 2482, 2486, 2489, 2493, 2493, 2510, 2510, 2524, 2525, 2527, 2529, 2544, 2545, 2556, 2556, 2565, 2570, 2575, 2576, 2579, 2600, 2602, 2608, 2610, 2611, 2613, 2614, 2616, 2617, 2649, 2652, 2654, 2654, 2674, 2676, 2693, 2701, 2703, 2705, 2707, 2728, 2730, 2736, 2738, 2739, 2741, 2745, 2749, 2749, 2768, 2768, 2784, 2785, 2809, 2809, 2821, 2828, 2831, 2832, 2835, 2856, 2858, 2864, 2866, 2867, 2869, 2873, 2877, 2877, 2908, 2909, 2911, 2913, 2929, 2929, 2947, 2947, 2949, 2954, 2958, 2960, 2962, 2965, 2969, 2970, 2972, 2972, 2974, 2975, 2979, 2980, 2984, 2986, 2990, 3001, 3024, 3024, 3077, 3084, 3086, 3088, 3090, 3112, 3114, 3129, 3133, 3133, 3160, 3162, 3168, 3169, 3200, 3200, 3205, 3212, 3214, 3216, 3218, 3240, 3242, 3251, 3253, 3257, 3261, 3261, 3294, 3294, 3296, 3297, 3313, 3314, 3333, 3340, 3342, 3344, 3346, 3386, 3389, 3389, 3406, 3406, 3412, 3414, 3423, 3425, 3450, 3455, 3461, 3478, 3482, 3505, 3507, 3515, 3517, 3517, 3520, 3526, 3585, 3632, 3634, 3635, 3648, 3654, 3713, 3714, 3716, 3716, 3718, 3722, 3724, 3747, 3749, 3749, 3751, 3760, 3762, 3763, 3773, 3773, 3776, 3780, 3782, 3782, 3804, 3807, 3840, 3840, 3904, 3911, 3913, 3948, 3976, 3980, 4096, 4138, 4159, 4159, 4176, 4181, 4186, 4189, 4193, 4193, 4197, 4198, 4206, 4208, 4213, 4225, 4238, 4238, 4256, 4293, 4295, 4295, 4301, 4301, 4304, 4346, 4348, 4680, 4682, 4685, 4688, 4694, 4696, 4696, 4698, 4701, 4704, 4744, 4746, 4749, 4752, 4784, 4786, 4789, 4792, 4798, 4800, 4800, 4802, 4805, 4808, 4822, 4824, 4880, 4882, 4885, 4888, 4954, 4992, 5007, 5024, 5109, 5112, 5117, 5121, 5740, 5743, 5759, 5761, 5786, 5792, 5866, 5870, 5880, 5888, 5900, 5902, 5905, 5920, 5937, 5952, 5969, 5984, 5996, 5998, 6000, 6016, 6067, 6103, 6103, 6108, 6108, 6176, 6264, 6272, 6312, 6314, 6314, 6320, 6389, 6400, 6430, 6480, 6509, 6512, 6516, 6528, 6571, 6576, 6601, 6656, 6678, 6688, 6740, 6823, 6823, 6917, 6963, 6981, 6987, 7043, 7072, 7086, 7087, 7098, 7141, 7168, 7203, 7245, 7247, 7258, 7293, 7296, 7304, 7312, 7354, 7357, 7359, 7401, 7404, 7406, 7411, 7413, 7414, 7418, 7418, 7424, 7615, 7680, 7957, 7960, 7965, 7968, 8005, 8008, 8013, 8016, 8023, 8025, 8025, 8027, 8027, 8029, 8029, 8031, 8061, 8064, 8116, 8118, 8124, 8126, 8126, 8130, 8132, 8134, 8140, 8144, 8147, 8150, 8155, 8160, 8172, 8178, 8180, 8182, 8188, 8305, 8305, 8319, 8319, 8336, 8348, 8450, 8450, 8455, 8455, 8458, 8467, 8469, 8469, 8472, 8477, 8484, 8484, 8486, 8486, 8488, 8488, 8490, 8505, 8508, 8511, 8517, 8521, 8526, 8526, 8544, 8584, 11264, 11310, 11312, 11358, 11360, 11492, 11499, 11502, 11506, 11507, 11520, 11557, 11559, 11559, 11565, 11565, 11568, 11623, 11631, 11631, 11648, 11670, 11680, 11686, 11688, 11694, 11696, 11702, 11704, 11710, 11712, 11718, 11720, 11726, 11728, 11734, 11736, 11742, 12293, 12295, 12321, 12329, 12337, 12341, 12344, 12348, 12353, 12438, 12443, 12447, 12449, 12538, 12540, 12543, 12549, 12591, 12593, 12686, 12704, 12730, 12784, 12799, 13312, 19893, 19968, 40943, 40960, 42124, 42192, 42237, 42240, 42508, 42512, 42527, 42538, 42539, 42560, 42606, 42623, 42653, 42656, 42735, 42775, 42783, 42786, 42888, 42891, 42943, 42946, 42950, 42999, 43009, 43011, 43013, 43015, 43018, 43020, 43042, 43072, 43123, 43138, 43187, 43250, 43255, 43259, 43259, 43261, 43262, 43274, 43301, 43312, 43334, 43360, 43388, 43396, 43442, 43471, 43471, 43488, 43492, 43494, 43503, 43514, 43518, 43520, 43560, 43584, 43586, 43588, 43595, 43616, 43638, 43642, 43642, 43646, 43695, 43697, 43697, 43701, 43702, 43705, 43709, 43712, 43712, 43714, 43714, 43739, 43741, 43744, 43754, 43762, 43764, 43777, 43782, 43785, 43790, 43793, 43798, 43808, 43814, 43816, 43822, 43824, 43866, 43868, 43879, 43888, 44002, 44032, 55203, 55216, 55238, 55243, 55291, 63744, 64109, 64112, 64217, 64256, 64262, 64275, 64279, 64285, 64285, 64287, 64296, 64298, 64310, 64312, 64316, 64318, 64318, 64320, 64321, 64323, 64324, 64326, 64433, 64467, 64829, 64848, 64911, 64914, 64967, 65008, 65019, 65136, 65140, 65142, 65276, 65313, 65338, 65345, 65370, 65382, 65470, 65474, 65479, 65482, 65487, 65490, 65495, 65498, 65500, 65536, 65547, 65549, 65574, 65576, 65594, 65596, 65597, 65599, 65613, 65616, 65629, 65664, 65786, 65856, 65908, 66176, 66204, 66208, 66256, 66304, 66335, 66349, 66378, 66384, 66421, 66432, 66461, 66464, 66499, 66504, 66511, 66513, 66517, 66560, 66717, 66736, 66771, 66776, 66811, 66816, 66855, 66864, 66915, 67072, 67382, 67392, 67413, 67424, 67431, 67584, 67589, 67592, 67592, 67594, 67637, 67639, 67640, 67644, 67644, 67647, 67669, 67680, 67702, 67712, 67742, 67808, 67826, 67828, 67829, 67840, 67861, 67872, 67897, 67968, 68023, 68030, 68031, 68096, 68096, 68112, 68115, 68117, 68119, 68121, 68149, 68192, 68220, 68224, 68252, 68288, 68295, 68297, 68324, 68352, 68405, 68416, 68437, 68448, 68466, 68480, 68497, 68608, 68680, 68736, 68786, 68800, 68850, 68864, 68899, 69376, 69404, 69415, 69415, 69424, 69445, 69600, 69622, 69635, 69687, 69763, 69807, 69840, 69864, 69891, 69926, 69956, 69956, 69968, 70002, 70006, 70006, 70019, 70066, 70081, 70084, 70106, 70106, 70108, 70108, 70144, 70161, 70163, 70187, 70272, 70278, 70280, 70280, 70282, 70285, 70287, 70301, 70303, 70312, 70320, 70366, 70405, 70412, 70415, 70416, 70419, 70440, 70442, 70448, 70450, 70451, 70453, 70457, 70461, 70461, 70480, 70480, 70493, 70497, 70656, 70708, 70727, 70730, 70751, 70751, 70784, 70831, 70852, 70853, 70855, 70855, 71040, 71086, 71128, 71131, 71168, 71215, 71236, 71236, 71296, 71338, 71352, 71352, 71424, 71450, 71680, 71723, 71840, 71903, 71935, 71935, 72096, 72103, 72106, 72144, 72161, 72161, 72163, 72163, 72192, 72192, 72203, 72242, 72250, 72250, 72272, 72272, 72284, 72329, 72349, 72349, 72384, 72440, 72704, 72712, 72714, 72750, 72768, 72768, 72818, 72847, 72960, 72966, 72968, 72969, 72971, 73008, 73030, 73030, 73056, 73061, 73063, 73064, 73066, 73097, 73112, 73112, 73440, 73458, 73728, 74649, 74752, 74862, 74880, 75075, 77824, 78894, 82944, 83526, 92160, 92728, 92736, 92766, 92880, 92909, 92928, 92975, 92992, 92995, 93027, 93047, 93053, 93071, 93760, 93823, 93952, 94026, 94032, 94032, 94099, 94111, 94176, 94177, 94179, 94179, 94208, 100343, 100352, 101106, 110592, 110878, 110928, 110930, 110948, 110951, 110960, 111355, 113664, 113770, 113776, 113788, 113792, 113800, 113808, 113817, 119808, 119892, 119894, 119964, 119966, 119967, 119970, 119970, 119973, 119974, 119977, 119980, 119982, 119993, 119995, 119995, 119997, 120003, 120005, 120069, 120071, 120074, 120077, 120084, 120086, 120092, 120094, 120121, 120123, 120126, 120128, 120132, 120134, 120134, 120138, 120144, 120146, 120485, 120488, 120512, 120514, 120538, 120540, 120570, 120572, 120596, 120598, 120628, 120630, 120654, 120656, 120686, 120688, 120712, 120714, 120744, 120746, 120770, 120772, 120779, 123136, 123180, 123191, 123197, 123214, 123214, 123584, 123627, 124928, 125124, 125184, 125251, 125259, 125259, 126464, 126467, 126469, 126495, 126497, 126498, 126500, 126500, 126503, 126503, 126505, 126514, 126516, 126519, 126521, 126521, 126523, 126523, 126530, 126530, 126535, 126535, 126537, 126537, 126539, 126539, 126541, 126543, 126545, 126546, 126548, 126548, 126551, 126551, 126553, 126553, 126555, 126555, 126557, 126557, 126559, 126559, 126561, 126562, 126564, 126564, 126567, 126570, 126572, 126578, 126580, 126583, 126585, 126588, 126590, 126590, 126592, 126601, 126603, 126619, 126625, 126627, 126629, 126633, 126635, 126651, 131072, 173782, 173824, 177972, 177984, 178205, 178208, 183969, 183984, 191456, 194560, 195101];
// dprint-ignore
const unicodeESNextIdentifierPart = [48, 57, 65, 90, 95, 95, 97, 122, 170, 170, 181, 181, 183, 183, 186, 186, 192, 214, 216, 246, 248, 705, 710, 721, 736, 740, 748, 748, 750, 750, 768, 884, 886, 887, 890, 893, 895, 895, 902, 906, 908, 908, 910, 929, 931, 1013, 1015, 1153, 1155, 1159, 1162, 1327, 1329, 1366, 1369, 1369, 1376, 1416, 1425, 1469, 1471, 1471, 1473, 1474, 1476, 1477, 1479, 1479, 1488, 1514, 1519, 1522, 1552, 1562, 1568, 1641, 1646, 1747, 1749, 1756, 1759, 1768, 1770, 1788, 1791, 1791, 1808, 1866, 1869, 1969, 1984, 2037, 2042, 2042, 2045, 2045, 2048, 2093, 2112, 2139, 2144, 2154, 2208, 2228, 2230, 2237, 2259, 2273, 2275, 2403, 2406, 2415, 2417, 2435, 2437, 2444, 2447, 2448, 2451, 2472, 2474, 2480, 2482, 2482, 2486, 2489, 2492, 2500, 2503, 2504, 2507, 2510, 2519, 2519, 2524, 2525, 2527, 2531, 2534, 2545, 2556, 2556, 2558, 2558, 2561, 2563, 2565, 2570, 2575, 2576, 2579, 2600, 2602, 2608, 2610, 2611, 2613, 2614, 2616, 2617, 2620, 2620, 2622, 2626, 2631, 2632, 2635, 2637, 2641, 2641, 2649, 2652, 2654, 2654, 2662, 2677, 2689, 2691, 2693, 2701, 2703, 2705, 2707, 2728, 2730, 2736, 2738, 2739, 2741, 2745, 2748, 2757, 2759, 2761, 2763, 2765, 2768, 2768, 2784, 2787, 2790, 2799, 2809, 2815, 2817, 2819, 2821, 2828, 2831, 2832, 2835, 2856, 2858, 2864, 2866, 2867, 2869, 2873, 2876, 2884, 2887, 2888, 2891, 2893, 2902, 2903, 2908, 2909, 2911, 2915, 2918, 2927, 2929, 2929, 2946, 2947, 2949, 2954, 2958, 2960, 2962, 2965, 2969, 2970, 2972, 2972, 2974, 2975, 2979, 2980, 2984, 2986, 2990, 3001, 3006, 3010, 3014, 3016, 3018, 3021, 3024, 3024, 3031, 3031, 3046, 3055, 3072, 3084, 3086, 3088, 3090, 3112, 3114, 3129, 3133, 3140, 3142, 3144, 3146, 3149, 3157, 3158, 3160, 3162, 3168, 3171, 3174, 3183, 3200, 3203, 3205, 3212, 3214, 3216, 3218, 3240, 3242, 3251, 3253, 3257, 3260, 3268, 3270, 3272, 3274, 3277, 3285, 3286, 3294, 3294, 3296, 3299, 3302, 3311, 3313, 3314, 3328, 3331, 3333, 3340, 3342, 3344, 3346, 3396, 3398, 3400, 3402, 3406, 3412, 3415, 3423, 3427, 3430, 3439, 3450, 3455, 3458, 3459, 3461, 3478, 3482, 3505, 3507, 3515, 3517, 3517, 3520, 3526, 3530, 3530, 3535, 3540, 3542, 3542, 3544, 3551, 3558, 3567, 3570, 3571, 3585, 3642, 3648, 3662, 3664, 3673, 3713, 3714, 3716, 3716, 3718, 3722, 3724, 3747, 3749, 3749, 3751, 3773, 3776, 3780, 3782, 3782, 3784, 3789, 3792, 3801, 3804, 3807, 3840, 3840, 3864, 3865, 3872, 3881, 3893, 3893, 3895, 3895, 3897, 3897, 3902, 3911, 3913, 3948, 3953, 3972, 3974, 3991, 3993, 4028, 4038, 4038, 4096, 4169, 4176, 4253, 4256, 4293, 4295, 4295, 4301, 4301, 4304, 4346, 4348, 4680, 4682, 4685, 4688, 4694, 4696, 4696, 4698, 4701, 4704, 4744, 4746, 4749, 4752, 4784, 4786, 4789, 4792, 4798, 4800, 4800, 4802, 4805, 4808, 4822, 4824, 4880, 4882, 4885, 4888, 4954, 4957, 4959, 4969, 4977, 4992, 5007, 5024, 5109, 5112, 5117, 5121, 5740, 5743, 5759, 5761, 5786, 5792, 5866, 5870, 5880, 5888, 5900, 5902, 5908, 5920, 5940, 5952, 5971, 5984, 5996, 5998, 6000, 6002, 6003, 6016, 6099, 6103, 6103, 6108, 6109, 6112, 6121, 6155, 6157, 6160, 6169, 6176, 6264, 6272, 6314, 6320, 6389, 6400, 6430, 6432, 6443, 6448, 6459, 6470, 6509, 6512, 6516, 6528, 6571, 6576, 6601, 6608, 6618, 6656, 6683, 6688, 6750, 6752, 6780, 6783, 6793, 6800, 6809, 6823, 6823, 6832, 6845, 6912, 6987, 6992, 7001, 7019, 7027, 7040, 7155, 7168, 7223, 7232, 7241, 7245, 7293, 7296, 7304, 7312, 7354, 7357, 7359, 7376, 7378, 7380, 7418, 7424, 7673, 7675, 7957, 7960, 7965, 7968, 8005, 8008, 8013, 8016, 8023, 8025, 8025, 8027, 8027, 8029, 8029, 8031, 8061, 8064, 8116, 8118, 8124, 8126, 8126, 8130, 8132, 8134, 8140, 8144, 8147, 8150, 8155, 8160, 8172, 8178, 8180, 8182, 8188, 8255, 8256, 8276, 8276, 8305, 8305, 8319, 8319, 8336, 8348, 8400, 8412, 8417, 8417, 8421, 8432, 8450, 8450, 8455, 8455, 8458, 8467, 8469, 8469, 8472, 8477, 8484, 8484, 8486, 8486, 8488, 8488, 8490, 8505, 8508, 8511, 8517, 8521, 8526, 8526, 8544, 8584, 11264, 11310, 11312, 11358, 11360, 11492, 11499, 11507, 11520, 11557, 11559, 11559, 11565, 11565, 11568, 11623, 11631, 11631, 11647, 11670, 11680, 11686, 11688, 11694, 11696, 11702, 11704, 11710, 11712, 11718, 11720, 11726, 11728, 11734, 11736, 11742, 11744, 11775, 12293, 12295, 12321, 12335, 12337, 12341, 12344, 12348, 12353, 12438, 12441, 12447, 12449, 12538, 12540, 12543, 12549, 12591, 12593, 12686, 12704, 12730, 12784, 12799, 13312, 19893, 19968, 40943, 40960, 42124, 42192, 42237, 42240, 42508, 42512, 42539, 42560, 42607, 42612, 42621, 42623, 42737, 42775, 42783, 42786, 42888, 42891, 42943, 42946, 42950, 42999, 43047, 43072, 43123, 43136, 43205, 43216, 43225, 43232, 43255, 43259, 43259, 43261, 43309, 43312, 43347, 43360, 43388, 43392, 43456, 43471, 43481, 43488, 43518, 43520, 43574, 43584, 43597, 43600, 43609, 43616, 43638, 43642, 43714, 43739, 43741, 43744, 43759, 43762, 43766, 43777, 43782, 43785, 43790, 43793, 43798, 43808, 43814, 43816, 43822, 43824, 43866, 43868, 43879, 43888, 44010, 44012, 44013, 44016, 44025, 44032, 55203, 55216, 55238, 55243, 55291, 63744, 64109, 64112, 64217, 64256, 64262, 64275, 64279, 64285, 64296, 64298, 64310, 64312, 64316, 64318, 64318, 64320, 64321, 64323, 64324, 64326, 64433, 64467, 64829, 64848, 64911, 64914, 64967, 65008, 65019, 65024, 65039, 65056, 65071, 65075, 65076, 65101, 65103, 65136, 65140, 65142, 65276, 65296, 65305, 65313, 65338, 65343, 65343, 65345, 65370, 65382, 65470, 65474, 65479, 65482, 65487, 65490, 65495, 65498, 65500, 65536, 65547, 65549, 65574, 65576, 65594, 65596, 65597, 65599, 65613, 65616, 65629, 65664, 65786, 65856, 65908, 66045, 66045, 66176, 66204, 66208, 66256, 66272, 66272, 66304, 66335, 66349, 66378, 66384, 66426, 66432, 66461, 66464, 66499, 66504, 66511, 66513, 66517, 66560, 66717, 66720, 66729, 66736, 66771, 66776, 66811, 66816, 66855, 66864, 66915, 67072, 67382, 67392, 67413, 67424, 67431, 67584, 67589, 67592, 67592, 67594, 67637, 67639, 67640, 67644, 67644, 67647, 67669, 67680, 67702, 67712, 67742, 67808, 67826, 67828, 67829, 67840, 67861, 67872, 67897, 67968, 68023, 68030, 68031, 68096, 68099, 68101, 68102, 68108, 68115, 68117, 68119, 68121, 68149, 68152, 68154, 68159, 68159, 68192, 68220, 68224, 68252, 68288, 68295, 68297, 68326, 68352, 68405, 68416, 68437, 68448, 68466, 68480, 68497, 68608, 68680, 68736, 68786, 68800, 68850, 68864, 68903, 68912, 68921, 69376, 69404, 69415, 69415, 69424, 69456, 69600, 69622, 69632, 69702, 69734, 69743, 69759, 69818, 69840, 69864, 69872, 69881, 69888, 69940, 69942, 69951, 69956, 69958, 69968, 70003, 70006, 70006, 70016, 70084, 70089, 70092, 70096, 70106, 70108, 70108, 70144, 70161, 70163, 70199, 70206, 70206, 70272, 70278, 70280, 70280, 70282, 70285, 70287, 70301, 70303, 70312, 70320, 70378, 70384, 70393, 70400, 70403, 70405, 70412, 70415, 70416, 70419, 70440, 70442, 70448, 70450, 70451, 70453, 70457, 70459, 70468, 70471, 70472, 70475, 70477, 70480, 70480, 70487, 70487, 70493, 70499, 70502, 70508, 70512, 70516, 70656, 70730, 70736, 70745, 70750, 70751, 70784, 70853, 70855, 70855, 70864, 70873, 71040, 71093, 71096, 71104, 71128, 71133, 71168, 71232, 71236, 71236, 71248, 71257, 71296, 71352, 71360, 71369, 71424, 71450, 71453, 71467, 71472, 71481, 71680, 71738, 71840, 71913, 71935, 71935, 72096, 72103, 72106, 72151, 72154, 72161, 72163, 72164, 72192, 72254, 72263, 72263, 72272, 72345, 72349, 72349, 72384, 72440, 72704, 72712, 72714, 72758, 72760, 72768, 72784, 72793, 72818, 72847, 72850, 72871, 72873, 72886, 72960, 72966, 72968, 72969, 72971, 73014, 73018, 73018, 73020, 73021, 73023, 73031, 73040, 73049, 73056, 73061, 73063, 73064, 73066, 73102, 73104, 73105, 73107, 73112, 73120, 73129, 73440, 73462, 73728, 74649, 74752, 74862, 74880, 75075, 77824, 78894, 82944, 83526, 92160, 92728, 92736, 92766, 92768, 92777, 92880, 92909, 92912, 92916, 92928, 92982, 92992, 92995, 93008, 93017, 93027, 93047, 93053, 93071, 93760, 93823, 93952, 94026, 94031, 94087, 94095, 94111, 94176, 94177, 94179, 94179, 94208, 100343, 100352, 101106, 110592, 110878, 110928, 110930, 110948, 110951, 110960, 111355, 113664, 113770, 113776, 113788, 113792, 113800, 113808, 113817, 113821, 113822, 119141, 119145, 119149, 119154, 119163, 119170, 119173, 119179, 119210, 119213, 119362, 119364, 119808, 119892, 119894, 119964, 119966, 119967, 119970, 119970, 119973, 119974, 119977, 119980, 119982, 119993, 119995, 119995, 119997, 120003, 120005, 120069, 120071, 120074, 120077, 120084, 120086, 120092, 120094, 120121, 120123, 120126, 120128, 120132, 120134, 120134, 120138, 120144, 120146, 120485, 120488, 120512, 120514, 120538, 120540, 120570, 120572, 120596, 120598, 120628, 120630, 120654, 120656, 120686, 120688, 120712, 120714, 120744, 120746, 120770, 120772, 120779, 120782, 120831, 121344, 121398, 121403, 121452, 121461, 121461, 121476, 121476, 121499, 121503, 121505, 121519, 122880, 122886, 122888, 122904, 122907, 122913, 122915, 122916, 122918, 122922, 123136, 123180, 123184, 123197, 123200, 123209, 123214, 123214, 123584, 123641, 124928, 125124, 125136, 125142, 125184, 125259, 125264, 125273, 126464, 126467, 126469, 126495, 126497, 126498, 126500, 126500, 126503, 126503, 126505, 126514, 126516, 126519, 126521, 126521, 126523, 126523, 126530, 126530, 126535, 126535, 126537, 126537, 126539, 126539, 126541, 126543, 126545, 126546, 126548, 126548, 126551, 126551, 126553, 126553, 126555, 126555, 126557, 126557, 126559, 126559, 126561, 126562, 126564, 126564, 126567, 126570, 126572, 126578, 126580, 126583, 126585, 126588, 126590, 126590, 126592, 126601, 126603, 126619, 126625, 126627, 126629, 126633, 126635, 126651, 131072, 173782, 173824, 177972, 177984, 178205, 178208, 183969, 183984, 191456, 194560, 195101, 917760, 917999];

/**
 * Test for whether a single line comment with leading whitespace trimmed's text contains a directive.
 */
const commentDirectiveRegExSingleLine = /^\/\/\/?\s*@(ts-expect-error|ts-ignore)/;

/**
 * Test for whether a multi-line comment with leading whitespace trimmed's last line contains a directive.
 */
const commentDirectiveRegExMultiLine = /^(?:\/|\*)*\s*@(ts-expect-error|ts-ignore)/;

const jsDocSeeOrLink = /@(?:see|link)/i;

function lookupInUnicodeMap(code: number, map: readonly number[]): boolean {
    // Bail out quickly if it couldn't possibly be in the map.
    if (code < map[0]) {
        return false;
    }

    // Perform binary search in one of the Unicode range maps
    let lo = 0;
    let hi: number = map.length;
    let mid: number;

    while (lo + 1 < hi) {
        mid = lo + (hi - lo) / 2;
        // mid has to be even to catch a range's beginning
        mid -= mid % 2;
        if (map[mid] <= code && code <= map[mid + 1]) {
            return true;
        }

        if (code < map[mid]) {
            hi = mid;
        }
        else {
            lo = mid + 2;
        }
    }

    return false;
}

/** @internal */ export function isUnicodeIdentifierStart(code: number, languageVersion: ScriptTarget | undefined) {
    return languageVersion! >= ScriptTarget.ES2015 ?
        lookupInUnicodeMap(code, unicodeESNextIdentifierStart) :
        languageVersion === ScriptTarget.ES5 ? lookupInUnicodeMap(code, unicodeES5IdentifierStart) :
        lookupInUnicodeMap(code, unicodeES3IdentifierStart);
}

function isUnicodeIdentifierPart(code: number, languageVersion: ScriptTarget | undefined) {
    return languageVersion! >= ScriptTarget.ES2015 ?
        lookupInUnicodeMap(code, unicodeESNextIdentifierPart) :
        languageVersion === ScriptTarget.ES5 ? lookupInUnicodeMap(code, unicodeES5IdentifierPart) :
        lookupInUnicodeMap(code, unicodeES3IdentifierPart);
}

function makeReverseMap(source: Map<string, number>): string[] {
    const result: string[] = [];
    source.forEach((value, name) => {
        result[value] = name;
    });
    return result;
}

const tokenStrings = makeReverseMap(textToToken);

/** @internal */
export function tokenToString(t: PunctuationOrKeywordSyntaxKind): string;
export function tokenToString(t: SyntaxKind): string | undefined;
export function tokenToString(t: SyntaxKind): string | undefined {
    return tokenStrings[t];
}

/** @internal */
export function stringToToken(s: string): SyntaxKind | undefined {
    return textToToken.get(s);
}

/** @internal */
export function computeLineStarts(text: string): number[] {
    const result: number[] = [];
    let pos = 0;
    let lineStart = 0;
    while (pos < text.length) {
        const ch = text.charCodeAt(pos);
        pos++;
        switch (ch) {
            case CharacterCodes.carriageReturn:
                if (text.charCodeAt(pos) === CharacterCodes.lineFeed) {
                    pos++;
                }
            // falls through
            case CharacterCodes.lineFeed:
                result.push(lineStart);
                lineStart = pos;
                break;
            default:
                if (ch > CharacterCodes.maxAsciiCharacter && isLineBreak(ch)) {
                    result.push(lineStart);
                    lineStart = pos;
                }
                break;
        }
    }
    result.push(lineStart);
    return result;
}

export function getPositionOfLineAndCharacter(sourceFile: SourceFileLike, line: number, character: number): number;
/** @internal */
export function getPositionOfLineAndCharacter(sourceFile: SourceFileLike, line: number, character: number, allowEdits?: true): number; // eslint-disable-line @typescript-eslint/unified-signatures
export function getPositionOfLineAndCharacter(sourceFile: SourceFileLike, line: number, character: number, allowEdits?: true): number {
    return sourceFile.getPositionOfLineAndCharacter ?
        sourceFile.getPositionOfLineAndCharacter(line, character, allowEdits) :
        computePositionOfLineAndCharacter(getLineStarts(sourceFile), line, character, sourceFile.text, allowEdits);
}

/** @internal */
export function computePositionOfLineAndCharacter(lineStarts: readonly number[], line: number, character: number, debugText?: string, allowEdits?: true): number {
    if (line < 0 || line >= lineStarts.length) {
        if (allowEdits) {
            // Clamp line to nearest allowable value
            line = line < 0 ? 0 : line >= lineStarts.length ? lineStarts.length - 1 : line;
        }
        else {
            Debug.fail(`Bad line number. Line: ${line}, lineStarts.length: ${lineStarts.length} , line map is correct? ${debugText !== undefined ? arraysEqual(lineStarts, computeLineStarts(debugText)) : "unknown"}`);
        }
    }

    const res = lineStarts[line] + character;
    if (allowEdits) {
        // Clamp to nearest allowable values to allow the underlying to be edited without crashing (accuracy is lost, instead)
        // TODO: Somehow track edits between file as it was during the creation of sourcemap we have and the current file and
        // apply them to the computed position to improve accuracy
        return res > lineStarts[line + 1] ? lineStarts[line + 1] : typeof debugText === "string" && res > debugText.length ? debugText.length : res;
    }
    if (line < lineStarts.length - 1) {
        Debug.assert(res < lineStarts[line + 1]);
    }
    else if (debugText !== undefined) {
        Debug.assert(res <= debugText.length); // Allow single character overflow for trailing newline
    }
    return res;
}

/** @internal */
export function getLineStarts(sourceFile: SourceFileLike): readonly number[] {
    return sourceFile.lineMap || (sourceFile.lineMap = computeLineStarts(sourceFile.text));
}

/** @internal */
export function computeLineAndCharacterOfPosition(lineStarts: readonly number[], position: number): LineAndCharacter {
    const lineNumber = computeLineOfPosition(lineStarts, position);
    return {
        line: lineNumber,
        character: position - lineStarts[lineNumber],
    };
}

/**
 * @internal
 * We assume the first line starts at position 0 and 'position' is non-negative.
 */
export function computeLineOfPosition(lineStarts: readonly number[], position: number, lowerBound?: number) {
    let lineNumber = binarySearch(lineStarts, position, identity, compareValues, lowerBound);
    if (lineNumber < 0) {
        // If the actual position was not found,
        // the binary search returns the 2's-complement of the next line start
        // e.g. if the line starts at [5, 10, 23, 80] and the position requested was 20
        // then the search will return -2.
        //
        // We want the index of the previous line start, so we subtract 1.
        // Review 2's-complement if this is confusing.
        lineNumber = ~lineNumber - 1;
        Debug.assert(lineNumber !== -1, "position cannot precede the beginning of the file");
    }
    return lineNumber;
}

/** @internal */
export function getLinesBetweenPositions(sourceFile: SourceFileLike, pos1: number, pos2: number) {
    if (pos1 === pos2) return 0;
    const lineStarts = getLineStarts(sourceFile);
    const lower = Math.min(pos1, pos2);
    const isNegative = lower === pos2;
    const upper = isNegative ? pos1 : pos2;
    const lowerLine = computeLineOfPosition(lineStarts, lower);
    const upperLine = computeLineOfPosition(lineStarts, upper, lowerLine);
    return isNegative ? lowerLine - upperLine : upperLine - lowerLine;
}

export function getLineAndCharacterOfPosition(sourceFile: SourceFileLike, position: number): LineAndCharacter {
    return computeLineAndCharacterOfPosition(getLineStarts(sourceFile), position);
}

export function isWhiteSpaceLike(ch: number): boolean {
    return isWhiteSpaceSingleLine(ch) || isLineBreak(ch);
}

/** Does not include line breaks. For that, see isWhiteSpaceLike. */
export function isWhiteSpaceSingleLine(ch: number): boolean {
    // Note: nextLine is in the Zs space, and should be considered to be a whitespace.
    // It is explicitly not a line-break as it isn't in the exact set specified by EcmaScript.
    return ch === CharacterCodes.space ||
        ch === CharacterCodes.tab ||
        ch === CharacterCodes.verticalTab ||
        ch === CharacterCodes.formFeed ||
        ch === CharacterCodes.nonBreakingSpace ||
        ch === CharacterCodes.nextLine ||
        ch === CharacterCodes.ogham ||
        ch >= CharacterCodes.enQuad && ch <= CharacterCodes.zeroWidthSpace ||
        ch === CharacterCodes.narrowNoBreakSpace ||
        ch === CharacterCodes.mathematicalSpace ||
        ch === CharacterCodes.ideographicSpace ||
        ch === CharacterCodes.byteOrderMark;
}

export function isLineBreak(ch: number): boolean {
    // ES5 7.3:
    // The ECMAScript line terminator characters are listed in Table 3.
    //     Table 3: Line Terminator Characters
    //     Code Unit Value     Name                    Formal Name
    //     \u000A              Line Feed               <LF>
    //     \u000D              Carriage Return         <CR>
    //     \u2028              Line separator          <LS>
    //     \u2029              Paragraph separator     <PS>
    // Only the characters in Table 3 are treated as line terminators. Other new line or line
    // breaking characters are treated as white space but not as line terminators.

    return ch === CharacterCodes.lineFeed ||
        ch === CharacterCodes.carriageReturn ||
        ch === CharacterCodes.lineSeparator ||
        ch === CharacterCodes.paragraphSeparator;
}

function isDigit(ch: number): boolean {
    return ch >= CharacterCodes._0 && ch <= CharacterCodes._9;
}

function isHexDigit(ch: number): boolean {
    return isDigit(ch) || ch >= CharacterCodes.A && ch <= CharacterCodes.F || ch >= CharacterCodes.a && ch <= CharacterCodes.f;
}

function isCodePoint(code: number): boolean {
    return code <= 0x10FFFF;
}

/** @internal */
export function isOctalDigit(ch: number): boolean {
    return ch >= CharacterCodes._0 && ch <= CharacterCodes._7;
}

export function couldStartTrivia(text: string, pos: number): boolean {
    // Keep in sync with skipTrivia
    const ch = text.charCodeAt(pos);
    switch (ch) {
        case CharacterCodes.carriageReturn:
        case CharacterCodes.lineFeed:
        case CharacterCodes.tab:
        case CharacterCodes.verticalTab:
        case CharacterCodes.formFeed:
        case CharacterCodes.space:
        case CharacterCodes.slash:
        // starts of normal trivia
        // falls through
        case CharacterCodes.lessThan:
        case CharacterCodes.bar:
        case CharacterCodes.equals:
        case CharacterCodes.greaterThan:
            // Starts of conflict marker trivia
            return true;
        case CharacterCodes.hash:
            // Only if its the beginning can we have #! trivia
            return pos === 0;
        default:
            return ch > CharacterCodes.maxAsciiCharacter;
    }
}

/** @internal */
export function skipTrivia(text: string, pos: number, stopAfterLineBreak?: boolean, stopAtComments?: boolean, inJSDoc?: boolean): number {
    if (positionIsSynthesized(pos)) {
        return pos;
    }

    let canConsumeStar = false;
    // Keep in sync with couldStartTrivia
    while (true) {
        const ch = text.charCodeAt(pos);
        switch (ch) {
            case CharacterCodes.carriageReturn:
                if (text.charCodeAt(pos + 1) === CharacterCodes.lineFeed) {
                    pos++;
                }
            // falls through
            case CharacterCodes.lineFeed:
                pos++;
                if (stopAfterLineBreak) {
                    return pos;
                }
                canConsumeStar = !!inJSDoc;
                continue;
            case CharacterCodes.tab:
            case CharacterCodes.verticalTab:
            case CharacterCodes.formFeed:
            case CharacterCodes.space:
                pos++;
                continue;
            case CharacterCodes.slash:
                if (stopAtComments) {
                    break;
                }
                if (text.charCodeAt(pos + 1) === CharacterCodes.slash) {
                    pos += 2;
                    while (pos < text.length) {
                        if (isLineBreak(text.charCodeAt(pos))) {
                            break;
                        }
                        pos++;
                    }
                    canConsumeStar = false;
                    continue;
                }
                if (text.charCodeAt(pos + 1) === CharacterCodes.asterisk) {
                    pos += 2;
                    while (pos < text.length) {
                        if (text.charCodeAt(pos) === CharacterCodes.asterisk && text.charCodeAt(pos + 1) === CharacterCodes.slash) {
                            pos += 2;
                            break;
                        }
                        pos++;
                    }
                    canConsumeStar = false;
                    continue;
                }
                break;

            case CharacterCodes.lessThan:
            case CharacterCodes.bar:
            case CharacterCodes.equals:
            case CharacterCodes.greaterThan:
                if (isConflictMarkerTrivia(text, pos)) {
                    pos = scanConflictMarkerTrivia(text, pos);
                    canConsumeStar = false;
                    continue;
                }
                break;

            case CharacterCodes.hash:
                if (pos === 0 && isShebangTrivia(text, pos)) {
                    pos = scanShebangTrivia(text, pos);
                    canConsumeStar = false;
                    continue;
                }
                break;

            case CharacterCodes.asterisk:
                if (canConsumeStar) {
                    pos++;
                    canConsumeStar = false;
                    continue;
                }
                break;

            default:
                if (ch > CharacterCodes.maxAsciiCharacter && (isWhiteSpaceLike(ch))) {
                    pos++;
                    continue;
                }
                break;
        }
        return pos;
    }
}

// All conflict markers consist of the same character repeated seven times.  If it is
// a <<<<<<< or >>>>>>> marker then it is also followed by a space.
const mergeConflictMarkerLength = "<<<<<<<".length;

function isConflictMarkerTrivia(text: string, pos: number) {
    Debug.assert(pos >= 0);

    // Conflict markers must be at the start of a line.
    if (pos === 0 || isLineBreak(text.charCodeAt(pos - 1))) {
        const ch = text.charCodeAt(pos);

        if ((pos + mergeConflictMarkerLength) < text.length) {
            for (let i = 0; i < mergeConflictMarkerLength; i++) {
                if (text.charCodeAt(pos + i) !== ch) {
                    return false;
                }
            }

            return ch === CharacterCodes.equals ||
                text.charCodeAt(pos + mergeConflictMarkerLength) === CharacterCodes.space;
        }
    }

    return false;
}

function scanConflictMarkerTrivia(text: string, pos: number, error?: (diag: DiagnosticMessage, pos?: number, len?: number) => void) {
    if (error) {
        error(Diagnostics.Merge_conflict_marker_encountered, pos, mergeConflictMarkerLength);
    }

    const ch = text.charCodeAt(pos);
    const len = text.length;

    if (ch === CharacterCodes.lessThan || ch === CharacterCodes.greaterThan) {
        while (pos < len && !isLineBreak(text.charCodeAt(pos))) {
            pos++;
        }
    }
    else {
        Debug.assert(ch === CharacterCodes.bar || ch === CharacterCodes.equals);
        // Consume everything from the start of a ||||||| or ======= marker to the start
        // of the next ======= or >>>>>>> marker.
        while (pos < len) {
            const currentChar = text.charCodeAt(pos);
            if ((currentChar === CharacterCodes.equals || currentChar === CharacterCodes.greaterThan) && currentChar !== ch && isConflictMarkerTrivia(text, pos)) {
                break;
            }

            pos++;
        }
    }

    return pos;
}

const shebangTriviaRegex = /^#!.*/;

/** @internal */
export function isShebangTrivia(text: string, pos: number) {
    // Shebangs check must only be done at the start of the file
    Debug.assert(pos === 0);
    return shebangTriviaRegex.test(text);
}

/** @internal */
export function scanShebangTrivia(text: string, pos: number) {
    const shebang = shebangTriviaRegex.exec(text)![0];
    pos = pos + shebang.length;
    return pos;
}

/**
 * Invokes a callback for each comment range following the provided position.
 *
 * Single-line comment ranges include the leading double-slash characters but not the ending
 * line break. Multi-line comment ranges include the leading slash-asterisk and trailing
 * asterisk-slash characters.
 *
 * @param reduce If true, accumulates the result of calling the callback in a fashion similar
 *      to reduceLeft. If false, iteration stops when the callback returns a truthy value.
 * @param text The source text to scan.
 * @param pos The position at which to start scanning.
 * @param trailing If false, whitespace is skipped until the first line break and comments
 *      between that location and the next token are returned. If true, comments occurring
 *      between the given position and the next line break are returned.
 * @param cb The callback to execute as each comment range is encountered.
 * @param state A state value to pass to each iteration of the callback.
 * @param initial An initial value to pass when accumulating results (when "reduce" is true).
 * @returns If "reduce" is true, the accumulated value. If "reduce" is false, the first truthy
 *      return value of the callback.
 */
function iterateCommentRanges<T, U>(reduce: boolean, text: string, pos: number, trailing: boolean, cb: (pos: number, end: number, kind: CommentKind, hasTrailingNewLine: boolean, state: T, memo: U | undefined) => U, state: T, initial?: U): U | undefined {
    let pendingPos!: number;
    let pendingEnd!: number;
    let pendingKind!: CommentKind;
    let pendingHasTrailingNewLine!: boolean;
    let hasPendingCommentRange = false;
    let collecting = trailing;
    let accumulator = initial;
    if (pos === 0) {
        collecting = true;
        const shebang = getShebang(text);
        if (shebang) {
            pos = shebang.length;
        }
    }
    scan:
    while (pos >= 0 && pos < text.length) {
        const ch = text.charCodeAt(pos);
        switch (ch) {
            case CharacterCodes.carriageReturn:
                if (text.charCodeAt(pos + 1) === CharacterCodes.lineFeed) {
                    pos++;
                }
            // falls through
            case CharacterCodes.lineFeed:
                pos++;
                if (trailing) {
                    break scan;
                }

                collecting = true;
                if (hasPendingCommentRange) {
                    pendingHasTrailingNewLine = true;
                }

                continue;
            case CharacterCodes.tab:
            case CharacterCodes.verticalTab:
            case CharacterCodes.formFeed:
            case CharacterCodes.space:
                pos++;
                continue;
            case CharacterCodes.slash:
                const nextChar = text.charCodeAt(pos + 1);
                let hasTrailingNewLine = false;
                if (nextChar === CharacterCodes.slash || nextChar === CharacterCodes.asterisk) {
                    const kind = nextChar === CharacterCodes.slash ? SyntaxKind.SingleLineCommentTrivia : SyntaxKind.MultiLineCommentTrivia;
                    const startPos = pos;
                    pos += 2;
                    if (nextChar === CharacterCodes.slash) {
                        while (pos < text.length) {
                            if (isLineBreak(text.charCodeAt(pos))) {
                                hasTrailingNewLine = true;
                                break;
                            }
                            pos++;
                        }
                    }
                    else {
                        while (pos < text.length) {
                            if (text.charCodeAt(pos) === CharacterCodes.asterisk && text.charCodeAt(pos + 1) === CharacterCodes.slash) {
                                pos += 2;
                                break;
                            }
                            pos++;
                        }
                    }

                    if (collecting) {
                        if (hasPendingCommentRange) {
                            accumulator = cb(pendingPos, pendingEnd, pendingKind, pendingHasTrailingNewLine, state, accumulator);
                            if (!reduce && accumulator) {
                                // If we are not reducing and we have a truthy result, return it.
                                return accumulator;
                            }
                        }

                        pendingPos = startPos;
                        pendingEnd = pos;
                        pendingKind = kind;
                        pendingHasTrailingNewLine = hasTrailingNewLine;
                        hasPendingCommentRange = true;
                    }

                    continue;
                }
                break scan;
            default:
                if (ch > CharacterCodes.maxAsciiCharacter && (isWhiteSpaceLike(ch))) {
                    if (hasPendingCommentRange && isLineBreak(ch)) {
                        pendingHasTrailingNewLine = true;
                    }
                    pos++;
                    continue;
                }
                break scan;
        }
    }

    if (hasPendingCommentRange) {
        accumulator = cb(pendingPos, pendingEnd, pendingKind, pendingHasTrailingNewLine, state, accumulator);
    }

    return accumulator;
}

export function forEachLeadingCommentRange<U>(text: string, pos: number, cb: (pos: number, end: number, kind: CommentKind, hasTrailingNewLine: boolean) => U): U | undefined;
export function forEachLeadingCommentRange<T, U>(text: string, pos: number, cb: (pos: number, end: number, kind: CommentKind, hasTrailingNewLine: boolean, state: T) => U, state: T): U | undefined;
export function forEachLeadingCommentRange<T, U>(text: string, pos: number, cb: (pos: number, end: number, kind: CommentKind, hasTrailingNewLine: boolean, state: T) => U, state?: T): U | undefined {
    return iterateCommentRanges(/*reduce*/ false, text, pos, /*trailing*/ false, cb, state!);
}

export function forEachTrailingCommentRange<U>(text: string, pos: number, cb: (pos: number, end: number, kind: CommentKind, hasTrailingNewLine: boolean) => U): U | undefined;
export function forEachTrailingCommentRange<T, U>(text: string, pos: number, cb: (pos: number, end: number, kind: CommentKind, hasTrailingNewLine: boolean, state: T) => U, state: T): U | undefined;
export function forEachTrailingCommentRange<T, U>(text: string, pos: number, cb: (pos: number, end: number, kind: CommentKind, hasTrailingNewLine: boolean, state: T) => U, state?: T): U | undefined {
    return iterateCommentRanges(/*reduce*/ false, text, pos, /*trailing*/ true, cb, state!);
}

export function reduceEachLeadingCommentRange<T, U>(text: string, pos: number, cb: (pos: number, end: number, kind: CommentKind, hasTrailingNewLine: boolean, state: T) => U, state: T, initial: U) {
    return iterateCommentRanges(/*reduce*/ true, text, pos, /*trailing*/ false, cb, state, initial);
}

export function reduceEachTrailingCommentRange<T, U>(text: string, pos: number, cb: (pos: number, end: number, kind: CommentKind, hasTrailingNewLine: boolean, state: T) => U, state: T, initial: U) {
    return iterateCommentRanges(/*reduce*/ true, text, pos, /*trailing*/ true, cb, state, initial);
}

function appendCommentRange(pos: number, end: number, kind: CommentKind, hasTrailingNewLine: boolean, _state: any, comments: CommentRange[] = []) {
    comments.push({ kind, pos, end, hasTrailingNewLine });
    return comments;
}

export function getLeadingCommentRanges(text: string, pos: number): CommentRange[] | undefined {
    return reduceEachLeadingCommentRange(text, pos, appendCommentRange, /*state*/ undefined, /*initial*/ undefined);
}

export function getTrailingCommentRanges(text: string, pos: number): CommentRange[] | undefined {
    return reduceEachTrailingCommentRange(text, pos, appendCommentRange, /*state*/ undefined, /*initial*/ undefined);
}

/** Optionally, get the shebang */
export function getShebang(text: string): string | undefined {
    const match = shebangTriviaRegex.exec(text);
    if (match) {
        return match[0];
    }
}

export function isIdentifierStart(ch: number, languageVersion: ScriptTarget | undefined): boolean {
    return ch >= CharacterCodes.A && ch <= CharacterCodes.Z || ch >= CharacterCodes.a && ch <= CharacterCodes.z ||
        ch === CharacterCodes.$ || ch === CharacterCodes._ ||
        ch > CharacterCodes.maxAsciiCharacter && isUnicodeIdentifierStart(ch, languageVersion);
}

export function isIdentifierPart(ch: number, languageVersion: ScriptTarget | undefined, identifierVariant?: LanguageVariant): boolean {
    return ch >= CharacterCodes.A && ch <= CharacterCodes.Z || ch >= CharacterCodes.a && ch <= CharacterCodes.z ||
        ch >= CharacterCodes._0 && ch <= CharacterCodes._9 || ch === CharacterCodes.$ || ch === CharacterCodes._ ||
        // "-" and ":" are valid in JSX Identifiers
        (identifierVariant === LanguageVariant.JSX ? (ch === CharacterCodes.minus || ch === CharacterCodes.colon) : false) ||
        ch > CharacterCodes.maxAsciiCharacter && isUnicodeIdentifierPart(ch, languageVersion);
}

/** @internal */
export function isIdentifierText(name: string, languageVersion: ScriptTarget | undefined, identifierVariant?: LanguageVariant): boolean {
    let ch = codePointAt(name, 0);
    if (!isIdentifierStart(ch, languageVersion)) {
        return false;
    }

    for (let i = charSize(ch); i < name.length; i += charSize(ch)) {
        if (!isIdentifierPart(ch = codePointAt(name, i), languageVersion, identifierVariant)) {
            return false;
        }
    }

    return true;
}

// Creates a scanner over a (possibly unspecified) range of a piece of text.
export function createScanner(languageVersion: ScriptTarget, skipTrivia: boolean, languageVariant = LanguageVariant.Standard, textInitial?: string, onError?: ErrorCallback, start?: number, length?: number): Scanner {
    // Why var? It avoids TDZ checks in the runtime which can be costly.
    // See: https://github.com/microsoft/TypeScript/issues/52924
    /* eslint-disable no-var */
    var text = textInitial!;

    // Current position (end position of text of current token)
    var pos: number;

    // end of text
    var end: number;

    // Start position of whitespace before current token
    var fullStartPos: number;

    // Start position of text of current token
    var tokenStart: number;

    var token: SyntaxKind;
    var tokenValue!: string;
    var tokenFlags: TokenFlags;

    var commentDirectives: CommentDirective[] | undefined;
    var inJSDocType = 0;

    var scriptKind = ScriptKind.Unknown;
    var jsDocParsingMode = JSDocParsingMode.ParseAll;

    setText(text, start, length);

    var scanner: Scanner = {
        getTokenFullStart: () => fullStartPos,
        getStartPos: () => fullStartPos,
        getTokenEnd: () => pos,
        getTextPos: () => pos,
        getToken: () => token,
        getTokenStart: () => tokenStart,
        getTokenPos: () => tokenStart,
        getTokenText: () => text.substring(tokenStart, pos),
        getTokenValue: () => tokenValue,
        hasUnicodeEscape: () => (tokenFlags & TokenFlags.UnicodeEscape) !== 0,
        hasExtendedUnicodeEscape: () => (tokenFlags & TokenFlags.ExtendedUnicodeEscape) !== 0,
        hasPrecedingLineBreak: () => (tokenFlags & TokenFlags.PrecedingLineBreak) !== 0,
        hasPrecedingJSDocComment: () => (tokenFlags & TokenFlags.PrecedingJSDocComment) !== 0,
        isIdentifier: () => token === SyntaxKind.Identifier || token > SyntaxKind.LastReservedWord,
        isReservedWord: () => token >= SyntaxKind.FirstReservedWord && token <= SyntaxKind.LastReservedWord,
        isUnterminated: () => (tokenFlags & TokenFlags.Unterminated) !== 0,
        getCommentDirectives: () => commentDirectives,
        getNumericLiteralFlags: () => tokenFlags & TokenFlags.NumericLiteralFlags,
        getTokenFlags: () => tokenFlags,
        reScanGreaterToken,
        reScanAsteriskEqualsToken,
        reScanSlashToken,
        reScanTemplateToken,
        reScanTemplateHeadOrNoSubstitutionTemplate,
        scanJsxIdentifier,
        scanJsxAttributeValue,
        reScanJsxAttributeValue,
        reScanJsxToken,
        reScanLessThanToken,
        reScanHashToken,
        reScanQuestionToken,
        reScanInvalidIdentifier,
        scanJsxToken,
        scanJsDocToken,
        scanJSDocCommentTextToken,
        scan,
        getText,
        clearCommentDirectives,
        setText,
        setScriptTarget,
        setLanguageVariant,
        setScriptKind,
        setJSDocParsingMode,
        setOnError,
        resetTokenState,
        setTextPos: resetTokenState,
        setInJSDocType,
        tryScan,
        lookAhead,
        scanRange,
    };
    /* eslint-enable no-var */

    if (Debug.isDebugging) {
        Object.defineProperty(scanner, "__debugShowCurrentPositionInText", {
            get: () => {
                const text = scanner.getText();
                return text.slice(0, scanner.getTokenFullStart()) + "║" + text.slice(scanner.getTokenFullStart());
            },
        });
    }

    return scanner;

    function error(message: DiagnosticMessage): void;
    function error(message: DiagnosticMessage, errPos: number, length: number, arg0?: any): void;
    function error(message: DiagnosticMessage, errPos: number = pos, length?: number, arg0?: any): void {
        if (onError) {
            const oldPos = pos;
            pos = errPos;
            onError(message, length || 0, arg0);
            pos = oldPos;
        }
    }

    function scanNumberFragment(): string {
        let start = pos;
        let allowSeparator = false;
        let isPreviousTokenSeparator = false;
        let result = "";
        while (true) {
            const ch = text.charCodeAt(pos);
            if (ch === CharacterCodes._) {
                tokenFlags |= TokenFlags.ContainsSeparator;
                if (allowSeparator) {
                    allowSeparator = false;
                    isPreviousTokenSeparator = true;
                    result += text.substring(start, pos);
                }
                else {
                    tokenFlags |= TokenFlags.ContainsInvalidSeparator;
                    if (isPreviousTokenSeparator) {
                        error(Diagnostics.Multiple_consecutive_numeric_separators_are_not_permitted, pos, 1);
                    }
                    else {
                        error(Diagnostics.Numeric_separators_are_not_allowed_here, pos, 1);
                    }
                }
                pos++;
                start = pos;
                continue;
            }
            if (isDigit(ch)) {
                allowSeparator = true;
                isPreviousTokenSeparator = false;
                pos++;
                continue;
            }
            break;
        }
        if (text.charCodeAt(pos - 1) === CharacterCodes._) {
            tokenFlags |= TokenFlags.ContainsInvalidSeparator;
            error(Diagnostics.Numeric_separators_are_not_allowed_here, pos - 1, 1);
        }
        return result + text.substring(start, pos);
    }

    // Extract from Section 12.9.3
    // NumericLiteral ::=
    //     | DecimalLiteral
    //     | DecimalBigIntegerLiteral
    //     | NonDecimalIntegerLiteral 'n'?
    //     | LegacyOctalIntegerLiteral
    // DecimalBigIntegerLiteral ::=
    //     | '0n'
    //     | [1-9] DecimalDigits? 'n'
    //     | [1-9] '_' DecimalDigits 'n'
    // DecimalLiteral ::=
    //     | DecimalIntegerLiteral? '.' DecimalDigits? ExponentPart?
    //     | '.' DecimalDigits ExponentPart?
    //     | DecimalIntegerLiteral ExponentPart?
    // DecimalIntegerLiteral ::=
    //     | '0'
    //     | [1-9] '_'? DecimalDigits
    //     | NonOctalDecimalIntegerLiteral
    // LegacyOctalIntegerLiteral ::= '0' [0-7]+
    // NonOctalDecimalIntegerLiteral ::= '0' [0-7]* [89] [0-9]*
    function scanNumber(): SyntaxKind {
        let start = pos;
        let mainFragment: string;
        if (text.charCodeAt(pos) === CharacterCodes._0) {
            pos++;
            if (text.charCodeAt(pos) === CharacterCodes._) {
                tokenFlags |= TokenFlags.ContainsSeparator | TokenFlags.ContainsInvalidSeparator;
                error(Diagnostics.Numeric_separators_are_not_allowed_here, pos, 1);
                // treat it as a normal number literal
                pos--;
                mainFragment = scanNumberFragment();
            }
            // Separators are not allowed in the below cases
            else if (!scanDigits()) {
                // NonOctalDecimalIntegerLiteral, emit error later
                // Separators in decimal and exponent parts are still allowed according to the spec
                tokenFlags |= TokenFlags.ContainsLeadingZero;
                mainFragment = "" + +tokenValue;
            }
            else if (!tokenValue) {
                // a single zero
                mainFragment = "0";
            }
            else {
                // LegacyOctalIntegerLiteral
                tokenValue = "" + parseInt(tokenValue, 8);
                tokenFlags |= TokenFlags.Octal;
                const withMinus = token === SyntaxKind.MinusToken;
                const literal = (withMinus ? "-" : "") + "0o" + (+tokenValue).toString(8);
                if (withMinus) start--;
                error(Diagnostics.Octal_literals_are_not_allowed_Use_the_syntax_0, start, pos - start, literal);
                return SyntaxKind.NumericLiteral;
            }
        }
        else {
            mainFragment = scanNumberFragment();
        }
        let decimalFragment: string | undefined;
        let scientificFragment: string | undefined;
        if (text.charCodeAt(pos) === CharacterCodes.dot) {
            pos++;
            decimalFragment = scanNumberFragment();
        }
        let end = pos;
        if (text.charCodeAt(pos) === CharacterCodes.E || text.charCodeAt(pos) === CharacterCodes.e) {
            pos++;
            tokenFlags |= TokenFlags.Scientific;
            if (text.charCodeAt(pos) === CharacterCodes.plus || text.charCodeAt(pos) === CharacterCodes.minus) pos++;
            const preNumericPart = pos;
            const finalFragment = scanNumberFragment();
            if (!finalFragment) {
                error(Diagnostics.Digit_expected);
            }
            else {
                scientificFragment = text.substring(end, preNumericPart) + finalFragment;
                end = pos;
            }
        }
        let result: string;
        if (tokenFlags & TokenFlags.ContainsSeparator) {
            result = mainFragment;
            if (decimalFragment) {
                result += "." + decimalFragment;
            }
            if (scientificFragment) {
                result += scientificFragment;
            }
        }
        else {
            result = text.substring(start, end); // No need to use all the fragments; no _ removal needed
        }

        if (tokenFlags & TokenFlags.ContainsLeadingZero) {
            error(Diagnostics.Decimals_with_leading_zeros_are_not_allowed, start, end - start);
            // if a literal has a leading zero, it must not be bigint
            tokenValue = "" + +result;
            return SyntaxKind.NumericLiteral;
        }

        if (decimalFragment !== undefined || tokenFlags & TokenFlags.Scientific) {
            checkForIdentifierStartAfterNumericLiteral(start, decimalFragment === undefined && !!(tokenFlags & TokenFlags.Scientific));
            // if value is not an integer, it can be safely coerced to a number
            tokenValue = "" + +result;
            return SyntaxKind.NumericLiteral;
        }
        else {
            tokenValue = result;
            const type = checkBigIntSuffix(); // if value is an integer, check whether it is a bigint
            checkForIdentifierStartAfterNumericLiteral(start);
            return type;
        }
    }

    function checkForIdentifierStartAfterNumericLiteral(numericStart: number, isScientific?: boolean) {
        if (!isIdentifierStart(codePointAt(text, pos), languageVersion)) {
            return;
        }

        const identifierStart = pos;
        const { length } = scanIdentifierParts();

        if (length === 1 && text[identifierStart] === "n") {
            if (isScientific) {
                error(Diagnostics.A_bigint_literal_cannot_use_exponential_notation, numericStart, identifierStart - numericStart + 1);
            }
            else {
                error(Diagnostics.A_bigint_literal_must_be_an_integer, numericStart, identifierStart - numericStart + 1);
            }
        }
        else {
            error(Diagnostics.An_identifier_or_keyword_cannot_immediately_follow_a_numeric_literal, identifierStart, length);
            pos = identifierStart;
        }
    }

    function scanDigits(): boolean {
        const start = pos;
        let isOctal = true;
        while (isDigit(text.charCodeAt(pos))) {
            if (!isOctalDigit(text.charCodeAt(pos))) {
                isOctal = false;
            }
            pos++;
        }
        tokenValue = text.substring(start, pos);
        return isOctal;
    }

    /**
     * Scans the given number of hexadecimal digits in the text,
     * returning -1 if the given number is unavailable.
     */
    function scanExactNumberOfHexDigits(count: number, canHaveSeparators: boolean): number {
        const valueString = scanHexDigits(/*minCount*/ count, /*scanAsManyAsPossible*/ false, canHaveSeparators);
        return valueString ? parseInt(valueString, 16) : -1;
    }

    /**
     * Scans as many hexadecimal digits as are available in the text,
     * returning "" if the given number of digits was unavailable.
     */
    function scanMinimumNumberOfHexDigits(count: number, canHaveSeparators: boolean): string {
        return scanHexDigits(/*minCount*/ count, /*scanAsManyAsPossible*/ true, canHaveSeparators);
    }

    function scanHexDigits(minCount: number, scanAsManyAsPossible: boolean, canHaveSeparators: boolean): string {
        let valueChars: number[] = [];
        let allowSeparator = false;
        let isPreviousTokenSeparator = false;
        while (valueChars.length < minCount || scanAsManyAsPossible) {
            let ch = text.charCodeAt(pos);
            if (canHaveSeparators && ch === CharacterCodes._) {
                tokenFlags |= TokenFlags.ContainsSeparator;
                if (allowSeparator) {
                    allowSeparator = false;
                    isPreviousTokenSeparator = true;
                }
                else if (isPreviousTokenSeparator) {
                    error(Diagnostics.Multiple_consecutive_numeric_separators_are_not_permitted, pos, 1);
                }
                else {
                    error(Diagnostics.Numeric_separators_are_not_allowed_here, pos, 1);
                }
                pos++;
                continue;
            }
            allowSeparator = canHaveSeparators;
            if (ch >= CharacterCodes.A && ch <= CharacterCodes.F) {
                ch += CharacterCodes.a - CharacterCodes.A; // standardize hex literals to lowercase
            }
            else if (
                !((ch >= CharacterCodes._0 && ch <= CharacterCodes._9) ||
                    (ch >= CharacterCodes.a && ch <= CharacterCodes.f))
            ) {
                break;
            }
            valueChars.push(ch);
            pos++;
            isPreviousTokenSeparator = false;
        }
        if (valueChars.length < minCount) {
            valueChars = [];
        }
        if (text.charCodeAt(pos - 1) === CharacterCodes._) {
            error(Diagnostics.Numeric_separators_are_not_allowed_here, pos - 1, 1);
        }
        return String.fromCharCode(...valueChars);
    }

    function scanString(jsxAttributeString = false): string {
        const quote = text.charCodeAt(pos);
        pos++;
        let result = "";
        let start = pos;
        while (true) {
            if (pos >= end) {
                result += text.substring(start, pos);
                tokenFlags |= TokenFlags.Unterminated;
                error(Diagnostics.Unterminated_string_literal);
                break;
            }
            const ch = text.charCodeAt(pos);
            if (ch === quote) {
                result += text.substring(start, pos);
                pos++;
                break;
            }
            if (ch === CharacterCodes.backslash && !jsxAttributeString) {
                result += text.substring(start, pos);
                result += scanEscapeSequence(/*shouldEmitInvalidEscapeError*/ true);
                start = pos;
                continue;
            }

            if ((ch === CharacterCodes.lineFeed || ch === CharacterCodes.carriageReturn) && !jsxAttributeString) {
                result += text.substring(start, pos);
                tokenFlags |= TokenFlags.Unterminated;
                error(Diagnostics.Unterminated_string_literal);
                break;
            }
            pos++;
        }
        return result;
    }

    /**
     * Sets the current 'tokenValue' and returns a NoSubstitutionTemplateLiteral or
     * a literal component of a TemplateExpression.
     */
    function scanTemplateAndSetTokenValue(shouldEmitInvalidEscapeError: boolean): SyntaxKind {
        const startedWithBacktick = text.charCodeAt(pos) === CharacterCodes.backtick;

        pos++;
        let start = pos;
        let contents = "";
        let resultingToken: SyntaxKind;

        while (true) {
            if (pos >= end) {
                contents += text.substring(start, pos);
                tokenFlags |= TokenFlags.Unterminated;
                error(Diagnostics.Unterminated_template_literal);
                resultingToken = startedWithBacktick ? SyntaxKind.NoSubstitutionTemplateLiteral : SyntaxKind.TemplateTail;
                break;
            }

            const currChar = text.charCodeAt(pos);

            // '`'
            if (currChar === CharacterCodes.backtick) {
                contents += text.substring(start, pos);
                pos++;
                resultingToken = startedWithBacktick ? SyntaxKind.NoSubstitutionTemplateLiteral : SyntaxKind.TemplateTail;
                break;
            }

            // '${'
            if (currChar === CharacterCodes.$ && pos + 1 < end && text.charCodeAt(pos + 1) === CharacterCodes.openBrace) {
                contents += text.substring(start, pos);
                pos += 2;
                resultingToken = startedWithBacktick ? SyntaxKind.TemplateHead : SyntaxKind.TemplateMiddle;
                break;
            }

            // Escape character
            if (currChar === CharacterCodes.backslash) {
                contents += text.substring(start, pos);
                contents += scanEscapeSequence(shouldEmitInvalidEscapeError);
                start = pos;
                continue;
            }

            // Speculated ECMAScript 6 Spec 11.8.6.1:
            // <CR><LF> and <CR> LineTerminatorSequences are normalized to <LF> for Template Values
            if (currChar === CharacterCodes.carriageReturn) {
                contents += text.substring(start, pos);
                pos++;

                if (pos < end && text.charCodeAt(pos) === CharacterCodes.lineFeed) {
                    pos++;
                }

                contents += "\n";
                start = pos;
                continue;
            }

            pos++;
        }

        Debug.assert(resultingToken !== undefined);

        tokenValue = contents;
        return resultingToken;
    }

    // Extract from Section A.1
    // EscapeSequence ::
    //     | CharacterEscapeSequence
    //     | 0 (?![0-9])
    //     | LegacyOctalEscapeSequence
    //     | NonOctalDecimalEscapeSequence
    //     | HexEscapeSequence
    //     | UnicodeEscapeSequence
    // LegacyOctalEscapeSequence ::=
    //     | '0' (?=[89])
    //     | [1-7] (?![0-7])
    //     | [0-3] [0-7] (?![0-7])
    //     | [4-7] [0-7]
    //     | [0-3] [0-7] [0-7]
    // NonOctalDecimalEscapeSequence ::= [89]
    function scanEscapeSequence(shouldEmitInvalidEscapeError?: boolean): string {
        const start = pos;
        pos++;
        if (pos >= end) {
            error(Diagnostics.Unexpected_end_of_text);
            return "";
        }
        const ch = text.charCodeAt(pos);
        pos++;
        switch (ch) {
            case CharacterCodes._0:
                // Although '0' preceding any digit is treated as LegacyOctalEscapeSequence,
                // '\08' should separately be interpreted as '\0' + '8'.
                if (pos >= end || !isDigit(text.charCodeAt(pos))) {
                    return "\0";
                }
            // '\01', '\011'
            // falls through
            case CharacterCodes._1:
            case CharacterCodes._2:
            case CharacterCodes._3:
                // '\1', '\17', '\177'
                if (pos < end && isOctalDigit(text.charCodeAt(pos))) {
                    pos++;
                }
            // '\17', '\177'
            // falls through
            case CharacterCodes._4:
            case CharacterCodes._5:
            case CharacterCodes._6:
            case CharacterCodes._7:
                // '\4', '\47' but not '\477'
                if (pos < end && isOctalDigit(text.charCodeAt(pos))) {
                    pos++;
                }
                // '\47'
                tokenFlags |= TokenFlags.ContainsInvalidEscape;
                if (shouldEmitInvalidEscapeError) {
                    const code = parseInt(text.substring(start + 1, pos), 8);
                    error(Diagnostics.Octal_escape_sequences_are_not_allowed_Use_the_syntax_0, start, pos - start, "\\x" + code.toString(16).padStart(2, "0"));
                    return String.fromCharCode(code);
                }
                return text.substring(start, pos);
            case CharacterCodes._8:
            case CharacterCodes._9:
                // the invalid '\8' and '\9'
                tokenFlags |= TokenFlags.ContainsInvalidEscape;
                if (shouldEmitInvalidEscapeError) {
                    error(Diagnostics.Escape_sequence_0_is_not_allowed, start, pos - start, text.substring(start, pos));
                    return String.fromCharCode(ch);
                }
                return text.substring(start, pos);
            case CharacterCodes.b:
                return "\b";
            case CharacterCodes.t:
                return "\t";
            case CharacterCodes.n:
                return "\n";
            case CharacterCodes.v:
                return "\v";
            case CharacterCodes.f:
                return "\f";
            case CharacterCodes.r:
                return "\r";
            case CharacterCodes.singleQuote:
                return "'";
            case CharacterCodes.doubleQuote:
                return '"';
            case CharacterCodes.u:
                if (pos < end && text.charCodeAt(pos) === CharacterCodes.openBrace) {
                    // '\u{DDDDDDDD}'
                    pos++;
                    const escapedValueString = scanMinimumNumberOfHexDigits(1, /*canHaveSeparators*/ false);
                    const escapedValue = escapedValueString ? parseInt(escapedValueString, 16) : -1;
                    // '\u{Not Code Point' or '\u{CodePoint'
                    if (escapedValue < 0) {
                        tokenFlags |= TokenFlags.ContainsInvalidEscape;
                        if (shouldEmitInvalidEscapeError) {
                            error(Diagnostics.Hexadecimal_digit_expected);
                        }
                        return text.substring(start, pos);
                    }
                    if (!isCodePoint(escapedValue)) {
                        tokenFlags |= TokenFlags.ContainsInvalidEscape;
                        if (shouldEmitInvalidEscapeError) {
                            error(Diagnostics.An_extended_Unicode_escape_value_must_be_between_0x0_and_0x10FFFF_inclusive);
                        }
                        return text.substring(start, pos);
                    }
                    if (pos >= end) {
                        tokenFlags |= TokenFlags.ContainsInvalidEscape;
                        if (shouldEmitInvalidEscapeError) {
                            error(Diagnostics.Unexpected_end_of_text);
                        }
                        return text.substring(start, pos);
                    }
                    if (text.charCodeAt(pos) !== CharacterCodes.closeBrace) {
                        tokenFlags |= TokenFlags.ContainsInvalidEscape;
                        if (shouldEmitInvalidEscapeError) {
                            error(Diagnostics.Unterminated_Unicode_escape_sequence);
                        }
                        return text.substring(start, pos);
                    }
                    pos++;
                    tokenFlags |= TokenFlags.ExtendedUnicodeEscape;
                    return utf16EncodeAsString(escapedValue);
                }
                // '\uDDDD'
                for (; pos < start + 6; pos++) {
                    if (!(pos < end && isHexDigit(text.charCodeAt(pos)))) {
                        tokenFlags |= TokenFlags.ContainsInvalidEscape;
                        if (shouldEmitInvalidEscapeError) {
                            error(Diagnostics.Hexadecimal_digit_expected);
                        }
                        return text.substring(start, pos);
                    }
                }
                tokenFlags |= TokenFlags.UnicodeEscape;
                return String.fromCharCode(parseInt(text.substring(start + 2, pos), 16));

            case CharacterCodes.x:
                // '\xDD'
                for (; pos < start + 4; pos++) {
                    if (!(pos < end && isHexDigit(text.charCodeAt(pos)))) {
                        tokenFlags |= TokenFlags.ContainsInvalidEscape;
                        if (shouldEmitInvalidEscapeError) {
                            error(Diagnostics.Hexadecimal_digit_expected);
                        }
                        return text.substring(start, pos);
                    }
                }
                tokenFlags |= TokenFlags.HexEscape;
                return String.fromCharCode(parseInt(text.substring(start + 2, pos), 16));

            // when encountering a LineContinuation (i.e. a backslash and a line terminator sequence),
            // the line terminator is interpreted to be "the empty code unit sequence".
            case CharacterCodes.carriageReturn:
                if (pos < end && text.charCodeAt(pos) === CharacterCodes.lineFeed) {
                    pos++;
                }
            // falls through
            case CharacterCodes.lineFeed:
            case CharacterCodes.lineSeparator:
            case CharacterCodes.paragraphSeparator:
                return "";
            default:
                return String.fromCharCode(ch);
        }
    }

    function scanExtendedUnicodeEscape(): string {
        const escapedValueString = scanMinimumNumberOfHexDigits(1, /*canHaveSeparators*/ false);
        const escapedValue = escapedValueString ? parseInt(escapedValueString, 16) : -1;
        let isInvalidExtendedEscape = false;

        // Validate the value of the digit
        if (escapedValue < 0) {
            error(Diagnostics.Hexadecimal_digit_expected);
            isInvalidExtendedEscape = true;
        }
        else if (escapedValue > 0x10FFFF) {
            error(Diagnostics.An_extended_Unicode_escape_value_must_be_between_0x0_and_0x10FFFF_inclusive);
            isInvalidExtendedEscape = true;
        }

        if (pos >= end) {
            error(Diagnostics.Unexpected_end_of_text);
            isInvalidExtendedEscape = true;
        }
        else if (text.charCodeAt(pos) === CharacterCodes.closeBrace) {
            // Only swallow the following character up if it's a '}'.
            pos++;
        }
        else {
            error(Diagnostics.Unterminated_Unicode_escape_sequence);
            isInvalidExtendedEscape = true;
        }

        if (isInvalidExtendedEscape) {
            return "";
        }

        return utf16EncodeAsString(escapedValue);
    }

    // Current character is known to be a backslash. Check for Unicode escape of the form '\uXXXX'
    // and return code point value if valid Unicode escape is found. Otherwise return -1.
    function peekUnicodeEscape(): number {
        if (pos + 5 < end && text.charCodeAt(pos + 1) === CharacterCodes.u) {
            const start = pos;
            pos += 2;
            const value = scanExactNumberOfHexDigits(4, /*canHaveSeparators*/ false);
            pos = start;
            return value;
        }
        return -1;
    }

    function peekExtendedUnicodeEscape(): number {
        if (codePointAt(text, pos + 1) === CharacterCodes.u && codePointAt(text, pos + 2) === CharacterCodes.openBrace) {
            const start = pos;
            pos += 3;
            const escapedValueString = scanMinimumNumberOfHexDigits(1, /*canHaveSeparators*/ false);
            const escapedValue = escapedValueString ? parseInt(escapedValueString, 16) : -1;
            pos = start;
            return escapedValue;
        }
        return -1;
    }

    function scanIdentifierParts(): string {
        let result = "";
        let start = pos;
        while (pos < end) {
            let ch = codePointAt(text, pos);
            if (isIdentifierPart(ch, languageVersion)) {
                pos += charSize(ch);
            }
            else if (ch === CharacterCodes.backslash) {
                ch = peekExtendedUnicodeEscape();
                if (ch >= 0 && isIdentifierPart(ch, languageVersion)) {
                    pos += 3;
                    tokenFlags |= TokenFlags.ExtendedUnicodeEscape;
                    result += scanExtendedUnicodeEscape();
                    start = pos;
                    continue;
                }
                ch = peekUnicodeEscape();
                if (!(ch >= 0 && isIdentifierPart(ch, languageVersion))) {
                    break;
                }
                tokenFlags |= TokenFlags.UnicodeEscape;
                result += text.substring(start, pos);
                result += utf16EncodeAsString(ch);
                // Valid Unicode escape is always six characters
                pos += 6;
                start = pos;
            }
            else {
                break;
            }
        }
        result += text.substring(start, pos);
        return result;
    }

    function getIdentifierToken(): SyntaxKind.Identifier | KeywordSyntaxKind {
        // Reserved words are between 2 and 12 characters long and start with a lowercase letter
        const len = tokenValue.length;
        if (len >= 2 && len <= 12) {
            const ch = tokenValue.charCodeAt(0);
            if (ch >= CharacterCodes.a && ch <= CharacterCodes.z) {
                const keyword = textToKeyword.get(tokenValue);
                if (keyword !== undefined) {
                    return token = keyword;
                }
            }
        }
        return token = SyntaxKind.Identifier;
    }

    function scanBinaryOrOctalDigits(base: 2 | 8): string {
        let value = "";
        // For counting number of digits; Valid binaryIntegerLiteral must have at least one binary digit following B or b.
        // Similarly valid octalIntegerLiteral must have at least one octal digit following o or O.
        let separatorAllowed = false;
        let isPreviousTokenSeparator = false;
        while (true) {
            const ch = text.charCodeAt(pos);
            // Numeric separators are allowed anywhere within a numeric literal, except not at the beginning, or following another separator
            if (ch === CharacterCodes._) {
                tokenFlags |= TokenFlags.ContainsSeparator;
                if (separatorAllowed) {
                    separatorAllowed = false;
                    isPreviousTokenSeparator = true;
                }
                else if (isPreviousTokenSeparator) {
                    error(Diagnostics.Multiple_consecutive_numeric_separators_are_not_permitted, pos, 1);
                }
                else {
                    error(Diagnostics.Numeric_separators_are_not_allowed_here, pos, 1);
                }
                pos++;
                continue;
            }
            separatorAllowed = true;
            if (!isDigit(ch) || ch - CharacterCodes._0 >= base) {
                break;
            }
            value += text[pos];
            pos++;
            isPreviousTokenSeparator = false;
        }
        if (text.charCodeAt(pos - 1) === CharacterCodes._) {
            // Literal ends with underscore - not allowed
            error(Diagnostics.Numeric_separators_are_not_allowed_here, pos - 1, 1);
        }
        return value;
    }

    function checkBigIntSuffix(): SyntaxKind {
        if (text.charCodeAt(pos) === CharacterCodes.n) {
            tokenValue += "n";
            // Use base 10 instead of base 2 or base 8 for shorter literals
            if (tokenFlags & TokenFlags.BinaryOrOctalSpecifier) {
                tokenValue = parsePseudoBigInt(tokenValue) + "n";
            }
            pos++;
            return SyntaxKind.BigIntLiteral;
        }
        else { // not a bigint, so can convert to number in simplified form
            // Number() may not support 0b or 0o, so use parseInt() instead
            const numericValue = tokenFlags & TokenFlags.BinarySpecifier
                ? parseInt(tokenValue.slice(2), 2) // skip "0b"
                : tokenFlags & TokenFlags.OctalSpecifier
                ? parseInt(tokenValue.slice(2), 8) // skip "0o"
                : +tokenValue;
            tokenValue = "" + numericValue;
            return SyntaxKind.NumericLiteral;
        }
    }

    function scan(): SyntaxKind {
        fullStartPos = pos;
        tokenFlags = TokenFlags.None;
        let asteriskSeen = false;
        while (true) {
            tokenStart = pos;
            if (pos >= end) {
                return token = SyntaxKind.EndOfFileToken;
            }

            const ch = codePointAt(text, pos);
            if (pos === 0) {
                // If a file isn't valid text at all, it will usually be apparent
                // in the first few characters because UTF-8 decode will fail and produce U+FFFD.
                // If that happens, just issue one error and refuse to try to scan further;
                // this is likely a binary file that cannot be parsed.
                //
                // It's safe to slice the text; U+FFFD can only be produced by an invalid decode,
                // so even if we cut a surrogate pair in half, they wouldn't be U+FFFD.
                if (text.slice(0, 256).includes("\uFFFD")) {
                    error(Diagnostics.File_appears_to_be_binary);
                    pos = end;
                    return token = SyntaxKind.NonTextFileMarkerTrivia;
                }
                // Special handling for shebang
                if (ch === CharacterCodes.hash && isShebangTrivia(text, pos)) {
                    pos = scanShebangTrivia(text, pos);
                    if (skipTrivia) {
                        continue;
                    }
                    else {
                        return token = SyntaxKind.ShebangTrivia;
                    }
                }
            }

            switch (ch) {
                case CharacterCodes.lineFeed:
                case CharacterCodes.carriageReturn:
                    tokenFlags |= TokenFlags.PrecedingLineBreak;
                    if (skipTrivia) {
                        pos++;
                        continue;
                    }
                    else {
                        if (ch === CharacterCodes.carriageReturn && pos + 1 < end && text.charCodeAt(pos + 1) === CharacterCodes.lineFeed) {
                            // consume both CR and LF
                            pos += 2;
                        }
                        else {
                            pos++;
                        }
                        return token = SyntaxKind.NewLineTrivia;
                    }
                case CharacterCodes.tab:
                case CharacterCodes.verticalTab:
                case CharacterCodes.formFeed:
                case CharacterCodes.space:
                case CharacterCodes.nonBreakingSpace:
                case CharacterCodes.ogham:
                case CharacterCodes.enQuad:
                case CharacterCodes.emQuad:
                case CharacterCodes.enSpace:
                case CharacterCodes.emSpace:
                case CharacterCodes.threePerEmSpace:
                case CharacterCodes.fourPerEmSpace:
                case CharacterCodes.sixPerEmSpace:
                case CharacterCodes.figureSpace:
                case CharacterCodes.punctuationSpace:
                case CharacterCodes.thinSpace:
                case CharacterCodes.hairSpace:
                case CharacterCodes.zeroWidthSpace:
                case CharacterCodes.narrowNoBreakSpace:
                case CharacterCodes.mathematicalSpace:
                case CharacterCodes.ideographicSpace:
                case CharacterCodes.byteOrderMark:
                    if (skipTrivia) {
                        pos++;
                        continue;
                    }
                    else {
                        while (pos < end && isWhiteSpaceSingleLine(text.charCodeAt(pos))) {
                            pos++;
                        }
                        return token = SyntaxKind.WhitespaceTrivia;
                    }
                case CharacterCodes.exclamation:
                    if (text.charCodeAt(pos + 1) === CharacterCodes.equals) {
                        if (text.charCodeAt(pos + 2) === CharacterCodes.equals) {
                            return pos += 3, token = SyntaxKind.ExclamationEqualsEqualsToken;
                        }
                        return pos += 2, token = SyntaxKind.ExclamationEqualsToken;
                    }
                    pos++;
                    return token = SyntaxKind.ExclamationToken;
                case CharacterCodes.doubleQuote:
                case CharacterCodes.singleQuote:
                    tokenValue = scanString();
                    return token = SyntaxKind.StringLiteral;
                case CharacterCodes.backtick:
                    return token = scanTemplateAndSetTokenValue(/*shouldEmitInvalidEscapeError*/ false);
                case CharacterCodes.percent:
                    if (text.charCodeAt(pos + 1) === CharacterCodes.equals) {
                        return pos += 2, token = SyntaxKind.PercentEqualsToken;
                    }
                    pos++;
                    return token = SyntaxKind.PercentToken;
                case CharacterCodes.ampersand:
                    if (text.charCodeAt(pos + 1) === CharacterCodes.ampersand) {
                        if (text.charCodeAt(pos + 2) === CharacterCodes.equals) {
                            return pos += 3, token = SyntaxKind.AmpersandAmpersandEqualsToken;
                        }
                        return pos += 2, token = SyntaxKind.AmpersandAmpersandToken;
                    }
                    if (text.charCodeAt(pos + 1) === CharacterCodes.equals) {
                        return pos += 2, token = SyntaxKind.AmpersandEqualsToken;
                    }
                    pos++;
                    return token = SyntaxKind.AmpersandToken;
                case CharacterCodes.openParen:
                    pos++;
                    return token = SyntaxKind.OpenParenToken;
                case CharacterCodes.closeParen:
                    pos++;
                    return token = SyntaxKind.CloseParenToken;
                case CharacterCodes.asterisk:
                    if (text.charCodeAt(pos + 1) === CharacterCodes.equals) {
                        return pos += 2, token = SyntaxKind.AsteriskEqualsToken;
                    }
                    if (text.charCodeAt(pos + 1) === CharacterCodes.asterisk) {
                        if (text.charCodeAt(pos + 2) === CharacterCodes.equals) {
                            return pos += 3, token = SyntaxKind.AsteriskAsteriskEqualsToken;
                        }
                        return pos += 2, token = SyntaxKind.AsteriskAsteriskToken;
                    }
                    pos++;
                    if (inJSDocType && !asteriskSeen && (tokenFlags & TokenFlags.PrecedingLineBreak)) {
                        // decoration at the start of a JSDoc comment line
                        asteriskSeen = true;
                        continue;
                    }
                    return token = SyntaxKind.AsteriskToken;
                case CharacterCodes.plus:
                    if (text.charCodeAt(pos + 1) === CharacterCodes.plus) {
                        return pos += 2, token = SyntaxKind.PlusPlusToken;
                    }
                    if (text.charCodeAt(pos + 1) === CharacterCodes.equals) {
                        return pos += 2, token = SyntaxKind.PlusEqualsToken;
                    }
                    pos++;
                    return token = SyntaxKind.PlusToken;
                case CharacterCodes.comma:
                    pos++;
                    return token = SyntaxKind.CommaToken;
                case CharacterCodes.minus:
                    if (text.charCodeAt(pos + 1) === CharacterCodes.minus) {
                        return pos += 2, token = SyntaxKind.MinusMinusToken;
                    }
                    if (text.charCodeAt(pos + 1) === CharacterCodes.equals) {
                        return pos += 2, token = SyntaxKind.MinusEqualsToken;
                    }
                    pos++;
                    return token = SyntaxKind.MinusToken;
                case CharacterCodes.dot:
                    if (isDigit(text.charCodeAt(pos + 1))) {
                        scanNumber();
                        return token = SyntaxKind.NumericLiteral;
                    }
                    if (text.charCodeAt(pos + 1) === CharacterCodes.dot && text.charCodeAt(pos + 2) === CharacterCodes.dot) {
                        return pos += 3, token = SyntaxKind.DotDotDotToken;
                    }
                    pos++;
                    return token = SyntaxKind.DotToken;
                case CharacterCodes.slash:
                    // Single-line comment
                    if (text.charCodeAt(pos + 1) === CharacterCodes.slash) {
                        pos += 2;

                        while (pos < end) {
                            if (isLineBreak(text.charCodeAt(pos))) {
                                break;
                            }
                            pos++;
                        }

                        commentDirectives = appendIfCommentDirective(
                            commentDirectives,
                            text.slice(tokenStart, pos),
                            commentDirectiveRegExSingleLine,
                            tokenStart,
                        );

                        if (skipTrivia) {
                            continue;
                        }
                        else {
                            return token = SyntaxKind.SingleLineCommentTrivia;
                        }
                    }
                    // Multi-line comment
                    if (text.charCodeAt(pos + 1) === CharacterCodes.asterisk) {
                        pos += 2;
                        const isJSDoc = text.charCodeAt(pos) === CharacterCodes.asterisk && text.charCodeAt(pos + 1) !== CharacterCodes.slash;

                        let commentClosed = false;
                        let lastLineStart = tokenStart;
                        while (pos < end) {
                            const ch = text.charCodeAt(pos);

                            if (ch === CharacterCodes.asterisk && text.charCodeAt(pos + 1) === CharacterCodes.slash) {
                                pos += 2;
                                commentClosed = true;
                                break;
                            }

                            pos++;

                            if (isLineBreak(ch)) {
                                lastLineStart = pos;
                                tokenFlags |= TokenFlags.PrecedingLineBreak;
                            }
                        }

                        if (isJSDoc && shouldParseJSDoc()) {
                            tokenFlags |= TokenFlags.PrecedingJSDocComment;
                        }

                        commentDirectives = appendIfCommentDirective(commentDirectives, text.slice(lastLineStart, pos), commentDirectiveRegExMultiLine, lastLineStart);

                        if (!commentClosed) {
                            error(Diagnostics.Asterisk_Slash_expected);
                        }

                        if (skipTrivia) {
                            continue;
                        }
                        else {
                            if (!commentClosed) {
                                tokenFlags |= TokenFlags.Unterminated;
                            }
                            return token = SyntaxKind.MultiLineCommentTrivia;
                        }
                    }

                    if (text.charCodeAt(pos + 1) === CharacterCodes.equals) {
                        return pos += 2, token = SyntaxKind.SlashEqualsToken;
                    }

                    pos++;
                    return token = SyntaxKind.SlashToken;

                case CharacterCodes._0:
                    if (pos + 2 < end && (text.charCodeAt(pos + 1) === CharacterCodes.X || text.charCodeAt(pos + 1) === CharacterCodes.x)) {
                        pos += 2;
                        tokenValue = scanMinimumNumberOfHexDigits(1, /*canHaveSeparators*/ true);
                        if (!tokenValue) {
                            error(Diagnostics.Hexadecimal_digit_expected);
                            tokenValue = "0";
                        }
                        tokenValue = "0x" + tokenValue;
                        tokenFlags |= TokenFlags.HexSpecifier;
                        return token = checkBigIntSuffix();
                    }
                    else if (pos + 2 < end && (text.charCodeAt(pos + 1) === CharacterCodes.B || text.charCodeAt(pos + 1) === CharacterCodes.b)) {
                        pos += 2;
                        tokenValue = scanBinaryOrOctalDigits(/* base */ 2);
                        if (!tokenValue) {
                            error(Diagnostics.Binary_digit_expected);
                            tokenValue = "0";
                        }
                        tokenValue = "0b" + tokenValue;
                        tokenFlags |= TokenFlags.BinarySpecifier;
                        return token = checkBigIntSuffix();
                    }
                    else if (pos + 2 < end && (text.charCodeAt(pos + 1) === CharacterCodes.O || text.charCodeAt(pos + 1) === CharacterCodes.o)) {
                        pos += 2;
                        tokenValue = scanBinaryOrOctalDigits(/* base */ 8);
                        if (!tokenValue) {
                            error(Diagnostics.Octal_digit_expected);
                            tokenValue = "0";
                        }
                        tokenValue = "0o" + tokenValue;
                        tokenFlags |= TokenFlags.OctalSpecifier;
                        return token = checkBigIntSuffix();
                    }
                // falls through
                case CharacterCodes._1:
                case CharacterCodes._2:
                case CharacterCodes._3:
                case CharacterCodes._4:
                case CharacterCodes._5:
                case CharacterCodes._6:
                case CharacterCodes._7:
                case CharacterCodes._8:
                case CharacterCodes._9:
                    return token = scanNumber();
                case CharacterCodes.colon:
                    pos++;
                    return token = SyntaxKind.ColonToken;
                case CharacterCodes.semicolon:
                    pos++;
                    return token = SyntaxKind.SemicolonToken;
                case CharacterCodes.lessThan:
                    if (isConflictMarkerTrivia(text, pos)) {
                        pos = scanConflictMarkerTrivia(text, pos, error);
                        if (skipTrivia) {
                            continue;
                        }
                        else {
                            return token = SyntaxKind.ConflictMarkerTrivia;
                        }
                    }

                    if (text.charCodeAt(pos + 1) === CharacterCodes.lessThan) {
                        if (text.charCodeAt(pos + 2) === CharacterCodes.equals) {
                            return pos += 3, token = SyntaxKind.LessThanLessThanEqualsToken;
                        }
                        return pos += 2, token = SyntaxKind.LessThanLessThanToken;
                    }
                    if (text.charCodeAt(pos + 1) === CharacterCodes.equals) {
                        return pos += 2, token = SyntaxKind.LessThanEqualsToken;
                    }
                    if (
                        languageVariant === LanguageVariant.JSX &&
                        text.charCodeAt(pos + 1) === CharacterCodes.slash &&
                        text.charCodeAt(pos + 2) !== CharacterCodes.asterisk
                    ) {
                        return pos += 2, token = SyntaxKind.LessThanSlashToken;
                    }
                    pos++;
                    return token = SyntaxKind.LessThanToken;
                case CharacterCodes.equals:
                    if (isConflictMarkerTrivia(text, pos)) {
                        pos = scanConflictMarkerTrivia(text, pos, error);
                        if (skipTrivia) {
                            continue;
                        }
                        else {
                            return token = SyntaxKind.ConflictMarkerTrivia;
                        }
                    }

                    if (text.charCodeAt(pos + 1) === CharacterCodes.equals) {
                        if (text.charCodeAt(pos + 2) === CharacterCodes.equals) {
                            return pos += 3, token = SyntaxKind.EqualsEqualsEqualsToken;
                        }
                        return pos += 2, token = SyntaxKind.EqualsEqualsToken;
                    }
                    if (text.charCodeAt(pos + 1) === CharacterCodes.greaterThan) {
                        return pos += 2, token = SyntaxKind.EqualsGreaterThanToken;
                    }
                    pos++;
                    return token = SyntaxKind.EqualsToken;
                case CharacterCodes.greaterThan:
                    if (isConflictMarkerTrivia(text, pos)) {
                        pos = scanConflictMarkerTrivia(text, pos, error);
                        if (skipTrivia) {
                            continue;
                        }
                        else {
                            return token = SyntaxKind.ConflictMarkerTrivia;
                        }
                    }

                    pos++;
                    return token = SyntaxKind.GreaterThanToken;
                case CharacterCodes.question:
                    if (text.charCodeAt(pos + 1) === CharacterCodes.dot && !isDigit(text.charCodeAt(pos + 2))) {
                        return pos += 2, token = SyntaxKind.QuestionDotToken;
                    }
                    if (text.charCodeAt(pos + 1) === CharacterCodes.question) {
                        if (text.charCodeAt(pos + 2) === CharacterCodes.equals) {
                            return pos += 3, token = SyntaxKind.QuestionQuestionEqualsToken;
                        }
                        return pos += 2, token = SyntaxKind.QuestionQuestionToken;
                    }
                    pos++;
                    return token = SyntaxKind.QuestionToken;
                case CharacterCodes.openBracket:
                    pos++;
                    return token = SyntaxKind.OpenBracketToken;
                case CharacterCodes.closeBracket:
                    pos++;
                    return token = SyntaxKind.CloseBracketToken;
                case CharacterCodes.caret:
                    if (text.charCodeAt(pos + 1) === CharacterCodes.equals) {
                        return pos += 2, token = SyntaxKind.CaretEqualsToken;
                    }
                    pos++;
                    return token = SyntaxKind.CaretToken;
                case CharacterCodes.openBrace:
                    pos++;
                    return token = SyntaxKind.OpenBraceToken;
                case CharacterCodes.bar:
                    if (isConflictMarkerTrivia(text, pos)) {
                        pos = scanConflictMarkerTrivia(text, pos, error);
                        if (skipTrivia) {
                            continue;
                        }
                        else {
                            return token = SyntaxKind.ConflictMarkerTrivia;
                        }
                    }

                    if (text.charCodeAt(pos + 1) === CharacterCodes.bar) {
                        if (text.charCodeAt(pos + 2) === CharacterCodes.equals) {
                            return pos += 3, token = SyntaxKind.BarBarEqualsToken;
                        }
                        return pos += 2, token = SyntaxKind.BarBarToken;
                    }
                    if (text.charCodeAt(pos + 1) === CharacterCodes.equals) {
                        return pos += 2, token = SyntaxKind.BarEqualsToken;
                    }
                    pos++;
                    return token = SyntaxKind.BarToken;
                case CharacterCodes.closeBrace:
                    pos++;
                    return token = SyntaxKind.CloseBraceToken;
                case CharacterCodes.tilde:
                    pos++;
                    return token = SyntaxKind.TildeToken;
                case CharacterCodes.at:
                    pos++;
                    return token = SyntaxKind.AtToken;
                case CharacterCodes.backslash:
                    const extendedCookedChar = peekExtendedUnicodeEscape();
                    if (extendedCookedChar >= 0 && isIdentifierStart(extendedCookedChar, languageVersion)) {
                        pos += 3;
                        tokenFlags |= TokenFlags.ExtendedUnicodeEscape;
                        tokenValue = scanExtendedUnicodeEscape() + scanIdentifierParts();
                        return token = getIdentifierToken();
                    }

                    const cookedChar = peekUnicodeEscape();
                    if (cookedChar >= 0 && isIdentifierStart(cookedChar, languageVersion)) {
                        pos += 6;
                        tokenFlags |= TokenFlags.UnicodeEscape;
                        tokenValue = String.fromCharCode(cookedChar) + scanIdentifierParts();
                        return token = getIdentifierToken();
                    }

                    error(Diagnostics.Invalid_character);
                    pos++;
                    return token = SyntaxKind.Unknown;
                case CharacterCodes.hash:
                    if (pos !== 0 && text[pos + 1] === "!") {
                        error(Diagnostics.can_only_be_used_at_the_start_of_a_file);
                        pos++;
                        return token = SyntaxKind.Unknown;
                    }

                    const charAfterHash = codePointAt(text, pos + 1);
                    if (charAfterHash === CharacterCodes.backslash) {
                        pos++;
                        const extendedCookedChar = peekExtendedUnicodeEscape();
                        if (extendedCookedChar >= 0 && isIdentifierStart(extendedCookedChar, languageVersion)) {
                            pos += 3;
                            tokenFlags |= TokenFlags.ExtendedUnicodeEscape;
                            tokenValue = "#" + scanExtendedUnicodeEscape() + scanIdentifierParts();
                            return token = SyntaxKind.PrivateIdentifier;
                        }

                        const cookedChar = peekUnicodeEscape();
                        if (cookedChar >= 0 && isIdentifierStart(cookedChar, languageVersion)) {
                            pos += 6;
                            tokenFlags |= TokenFlags.UnicodeEscape;
                            tokenValue = "#" + String.fromCharCode(cookedChar) + scanIdentifierParts();
                            return token = SyntaxKind.PrivateIdentifier;
                        }
                        pos--;
                    }

                    if (isIdentifierStart(charAfterHash, languageVersion)) {
                        pos++;
                        // We're relying on scanIdentifier's behavior and adjusting the token kind after the fact.
                        // Notably absent from this block is the fact that calling a function named "scanIdentifier",
                        // but identifiers don't include '#', and that function doesn't deal with it at all.
                        // This works because 'scanIdentifier' tries to reuse source characters and builds up substrings;
                        // however, it starts at the 'tokenPos' which includes the '#', and will "accidentally" prepend the '#' for us.
                        scanIdentifier(charAfterHash, languageVersion);
                    }
                    else {
                        tokenValue = "#";
                        error(Diagnostics.Invalid_character, pos++, charSize(ch));
                    }
                    return token = SyntaxKind.PrivateIdentifier;
                default:
                    const identifierKind = scanIdentifier(ch, languageVersion);
                    if (identifierKind) {
                        return token = identifierKind;
                    }
                    else if (isWhiteSpaceSingleLine(ch)) {
                        pos += charSize(ch);
                        continue;
                    }
                    else if (isLineBreak(ch)) {
                        tokenFlags |= TokenFlags.PrecedingLineBreak;
                        pos += charSize(ch);
                        continue;
                    }
                    const size = charSize(ch);
                    error(Diagnostics.Invalid_character, pos, size);
                    pos += size;
                    return token = SyntaxKind.Unknown;
            }
        }
    }

    function shouldParseJSDoc() {
        switch (jsDocParsingMode) {
            case JSDocParsingMode.ParseAll:
                return true;
            case JSDocParsingMode.ParseNone:
                return false;
        }

        if (scriptKind !== ScriptKind.TS && scriptKind !== ScriptKind.TSX) {
            // If outside of TS, we need JSDoc to get any type info.
            return true;
        }

        if (jsDocParsingMode === JSDocParsingMode.ParseForTypeInfo) {
            // If we're in TS, but we don't need to produce reliable errors,
            // we don't need to parse to find @see or @link.
            return false;
        }

        return jsDocSeeOrLink.test(text.slice(fullStartPos, pos));
    }

    function reScanInvalidIdentifier(): SyntaxKind {
        Debug.assert(token === SyntaxKind.Unknown, "'reScanInvalidIdentifier' should only be called when the current token is 'SyntaxKind.Unknown'.");
        pos = tokenStart = fullStartPos;
        tokenFlags = 0;
        const ch = codePointAt(text, pos);
        const identifierKind = scanIdentifier(ch, ScriptTarget.ESNext);
        if (identifierKind) {
            return token = identifierKind;
        }
        pos += charSize(ch);
        return token; // Still `SyntaKind.Unknown`
    }

    function scanIdentifier(startCharacter: number, languageVersion: ScriptTarget) {
        let ch = startCharacter;
        if (isIdentifierStart(ch, languageVersion)) {
            pos += charSize(ch);
            while (pos < end && isIdentifierPart(ch = codePointAt(text, pos), languageVersion)) pos += charSize(ch);
            tokenValue = text.substring(tokenStart, pos);
            if (ch === CharacterCodes.backslash) {
                tokenValue += scanIdentifierParts();
            }
            return getIdentifierToken();
        }
    }

    function reScanGreaterToken(): SyntaxKind {
        if (token === SyntaxKind.GreaterThanToken) {
            if (text.charCodeAt(pos) === CharacterCodes.greaterThan) {
                if (text.charCodeAt(pos + 1) === CharacterCodes.greaterThan) {
                    if (text.charCodeAt(pos + 2) === CharacterCodes.equals) {
                        return pos += 3, token = SyntaxKind.GreaterThanGreaterThanGreaterThanEqualsToken;
                    }
                    return pos += 2, token = SyntaxKind.GreaterThanGreaterThanGreaterThanToken;
                }
                if (text.charCodeAt(pos + 1) === CharacterCodes.equals) {
                    return pos += 2, token = SyntaxKind.GreaterThanGreaterThanEqualsToken;
                }
                pos++;
                return token = SyntaxKind.GreaterThanGreaterThanToken;
            }
            if (text.charCodeAt(pos) === CharacterCodes.equals) {
                pos++;
                return token = SyntaxKind.GreaterThanEqualsToken;
            }
        }
        return token;
    }

    function reScanAsteriskEqualsToken(): SyntaxKind {
        Debug.assert(token === SyntaxKind.AsteriskEqualsToken, "'reScanAsteriskEqualsToken' should only be called on a '*='");
        pos = tokenStart + 1;
        return token = SyntaxKind.EqualsToken;
    }

    function reScanSlashToken(): SyntaxKind {
        if (token === SyntaxKind.SlashToken || token === SyntaxKind.SlashEqualsToken) {
            let p = tokenStart + 1;
            let inEscape = false;
            let inCharacterClass = false;
            while (true) {
                // If we reach the end of a file, or hit a newline, then this is an unterminated
                // regex.  Report error and return what we have so far.
                if (p >= end) {
                    tokenFlags |= TokenFlags.Unterminated;
                    error(Diagnostics.Unterminated_regular_expression_literal);
                    break;
                }

                const ch = text.charCodeAt(p);
                if (isLineBreak(ch)) {
                    tokenFlags |= TokenFlags.Unterminated;
                    error(Diagnostics.Unterminated_regular_expression_literal);
                    break;
                }

                if (inEscape) {
                    // Parsing an escape character;
                    // reset the flag and just advance to the next char.
                    inEscape = false;
                }
                else if (ch === CharacterCodes.slash && !inCharacterClass) {
                    // A slash within a character class is permissible,
                    // but in general it signals the end of the regexp literal.
                    p++;
                    break;
                }
                else if (ch === CharacterCodes.openBracket) {
                    inCharacterClass = true;
                }
                else if (ch === CharacterCodes.backslash) {
                    inEscape = true;
                }
                else if (ch === CharacterCodes.closeBracket) {
                    inCharacterClass = false;
                }
                p++;
            }

            while (p < end && isIdentifierPart(text.charCodeAt(p), languageVersion)) {
                p++;
            }
            pos = p;
            tokenValue = text.substring(tokenStart, pos);
            token = SyntaxKind.RegularExpressionLiteral;
        }
        return token;
    }

    function appendIfCommentDirective(
        commentDirectives: CommentDirective[] | undefined,
        text: string,
        commentDirectiveRegEx: RegExp,
        lineStart: number,
    ) {
        const type = getDirectiveFromComment(text.trimStart(), commentDirectiveRegEx);
        if (type === undefined) {
            return commentDirectives;
        }

        return append(
            commentDirectives,
            {
                range: { pos: lineStart, end: pos },
                type,
            },
        );
    }

    function getDirectiveFromComment(text: string, commentDirectiveRegEx: RegExp) {
        const match = commentDirectiveRegEx.exec(text);
        if (!match) {
            return undefined;
        }

        switch (match[1]) {
            case "ts-expect-error":
                return CommentDirectiveType.ExpectError;

            case "ts-ignore":
                return CommentDirectiveType.Ignore;
        }

        return undefined;
    }

    /**
     * Unconditionally back up and scan a template expression portion.
     */
    function reScanTemplateToken(isTaggedTemplate: boolean): SyntaxKind {
        pos = tokenStart;
        return token = scanTemplateAndSetTokenValue(!isTaggedTemplate);
    }

    function reScanTemplateHeadOrNoSubstitutionTemplate(): SyntaxKind {
        pos = tokenStart;
        return token = scanTemplateAndSetTokenValue(/*shouldEmitInvalidEscapeError*/ true);
    }

    function reScanJsxToken(allowMultilineJsxText = true): JsxTokenSyntaxKind {
        pos = tokenStart = fullStartPos;
        return token = scanJsxToken(allowMultilineJsxText);
    }

    function reScanLessThanToken(): SyntaxKind {
        if (token === SyntaxKind.LessThanLessThanToken) {
            pos = tokenStart + 1;
            return token = SyntaxKind.LessThanToken;
        }
        return token;
    }

    function reScanHashToken(): SyntaxKind {
        if (token === SyntaxKind.PrivateIdentifier) {
            pos = tokenStart + 1;
            return token = SyntaxKind.HashToken;
        }
        return token;
    }

    function reScanQuestionToken(): SyntaxKind {
        Debug.assert(token === SyntaxKind.QuestionQuestionToken, "'reScanQuestionToken' should only be called on a '??'");
        pos = tokenStart + 1;
        return token = SyntaxKind.QuestionToken;
    }

    function scanJsxToken(allowMultilineJsxText = true): JsxTokenSyntaxKind {
        fullStartPos = tokenStart = pos;

        if (pos >= end) {
            return token = SyntaxKind.EndOfFileToken;
        }

        let char = text.charCodeAt(pos);
        if (char === CharacterCodes.lessThan) {
            if (text.charCodeAt(pos + 1) === CharacterCodes.slash) {
                pos += 2;
                return token = SyntaxKind.LessThanSlashToken;
            }
            pos++;
            return token = SyntaxKind.LessThanToken;
        }

        if (char === CharacterCodes.openBrace) {
            pos++;
            return token = SyntaxKind.OpenBraceToken;
        }

        // First non-whitespace character on this line.
        let firstNonWhitespace = 0;

        // These initial values are special because the first line is:
        // firstNonWhitespace = 0 to indicate that we want leading whitespace,

        while (pos < end) {
            char = text.charCodeAt(pos);
            if (char === CharacterCodes.openBrace) {
                break;
            }
            if (char === CharacterCodes.lessThan) {
                if (isConflictMarkerTrivia(text, pos)) {
                    pos = scanConflictMarkerTrivia(text, pos, error);
                    return token = SyntaxKind.ConflictMarkerTrivia;
                }
                break;
            }
            if (char === CharacterCodes.greaterThan) {
                error(Diagnostics.Unexpected_token_Did_you_mean_or_gt, pos, 1);
            }
            if (char === CharacterCodes.closeBrace) {
                error(Diagnostics.Unexpected_token_Did_you_mean_or_rbrace, pos, 1);
            }

            // FirstNonWhitespace is 0, then we only see whitespaces so far. If we see a linebreak, we want to ignore that whitespaces.
            // i.e (- : whitespace)
            //      <div>----
            //      </div> becomes <div></div>
            //
            //      <div>----</div> becomes <div>----</div>
            if (isLineBreak(char) && firstNonWhitespace === 0) {
                firstNonWhitespace = -1;
            }
            else if (!allowMultilineJsxText && isLineBreak(char) && firstNonWhitespace > 0) {
                // Stop JsxText on each line during formatting. This allows the formatter to
                // indent each line correctly.
                break;
            }
            else if (!isWhiteSpaceLike(char)) {
                firstNonWhitespace = pos;
            }

            pos++;
        }

        tokenValue = text.substring(fullStartPos, pos);

        return firstNonWhitespace === -1 ? SyntaxKind.JsxTextAllWhiteSpaces : SyntaxKind.JsxText;
    }

    // Scans a JSX identifier; these differ from normal identifiers in that
    // they allow dashes
    function scanJsxIdentifier(): SyntaxKind {
        if (tokenIsIdentifierOrKeyword(token)) {
            // An identifier or keyword has already been parsed - check for a `-` or a single instance of `:` and then append it and
            // everything after it to the token
            // Do note that this means that `scanJsxIdentifier` effectively _mutates_ the visible token without advancing to a new token
            // Any caller should be expecting this behavior and should only read the pos or token value after calling it.
            while (pos < end) {
                const ch = text.charCodeAt(pos);
                if (ch === CharacterCodes.minus) {
                    tokenValue += "-";
                    pos++;
                    continue;
                }
                const oldPos = pos;
                tokenValue += scanIdentifierParts(); // reuse `scanIdentifierParts` so unicode escapes are handled
                if (pos === oldPos) {
                    break;
                }
            }
            return getIdentifierToken();
        }
        return token;
    }

    function scanJsxAttributeValue(): SyntaxKind {
        fullStartPos = pos;

        switch (text.charCodeAt(pos)) {
            case CharacterCodes.doubleQuote:
            case CharacterCodes.singleQuote:
                tokenValue = scanString(/*jsxAttributeString*/ true);
                return token = SyntaxKind.StringLiteral;
            default:
                // If this scans anything other than `{`, it's a parse error.
                return scan();
        }
    }

    function reScanJsxAttributeValue(): SyntaxKind {
        pos = tokenStart = fullStartPos;
        return scanJsxAttributeValue();
    }

    function scanJSDocCommentTextToken(inBackticks: boolean): JSDocSyntaxKind | SyntaxKind.JSDocCommentTextToken {
        fullStartPos = tokenStart = pos;
        tokenFlags = TokenFlags.None;
        if (pos >= end) {
            return token = SyntaxKind.EndOfFileToken;
        }
        for (let ch = text.charCodeAt(pos); pos < end && (!isLineBreak(ch) && ch !== CharacterCodes.backtick); ch = codePointAt(text, ++pos)) {
            if (!inBackticks) {
                if (ch === CharacterCodes.openBrace) {
                    break;
                }
                else if (
                    ch === CharacterCodes.at
                    && pos - 1 >= 0 && isWhiteSpaceSingleLine(text.charCodeAt(pos - 1))
                    && !(pos + 1 < end && isWhiteSpaceLike(text.charCodeAt(pos + 1)))
                ) {
                    // @ doesn't start a new tag inside ``, and elsewhere, only after whitespace and before non-whitespace
                    break;
                }
            }
        }
        if (pos === tokenStart) {
            return scanJsDocToken();
        }
        tokenValue = text.substring(tokenStart, pos);
        return token = SyntaxKind.JSDocCommentTextToken;
    }

    function scanJsDocToken(): JSDocSyntaxKind {
        fullStartPos = tokenStart = pos;
        tokenFlags = TokenFlags.None;
        if (pos >= end) {
            return token = SyntaxKind.EndOfFileToken;
        }

        const ch = codePointAt(text, pos);
        pos += charSize(ch);
        switch (ch) {
            case CharacterCodes.tab:
            case CharacterCodes.verticalTab:
            case CharacterCodes.formFeed:
            case CharacterCodes.space:
                while (pos < end && isWhiteSpaceSingleLine(text.charCodeAt(pos))) {
                    pos++;
                }
                return token = SyntaxKind.WhitespaceTrivia;
            case CharacterCodes.at:
                return token = SyntaxKind.AtToken;
            case CharacterCodes.carriageReturn:
                if (text.charCodeAt(pos) === CharacterCodes.lineFeed) {
                    pos++;
                }
                // falls through
            case CharacterCodes.lineFeed:
                tokenFlags |= TokenFlags.PrecedingLineBreak;
                return token = SyntaxKind.NewLineTrivia;
            case CharacterCodes.asterisk:
                return token = SyntaxKind.AsteriskToken;
            case CharacterCodes.openBrace:
                return token = SyntaxKind.OpenBraceToken;
            case CharacterCodes.closeBrace:
                return token = SyntaxKind.CloseBraceToken;
            case CharacterCodes.openBracket:
                return token = SyntaxKind.OpenBracketToken;
            case CharacterCodes.closeBracket:
                return token = SyntaxKind.CloseBracketToken;
            case CharacterCodes.lessThan:
                return token = SyntaxKind.LessThanToken;
            case CharacterCodes.greaterThan:
                return token = SyntaxKind.GreaterThanToken;
            case CharacterCodes.equals:
                return token = SyntaxKind.EqualsToken;
            case CharacterCodes.comma:
                return token = SyntaxKind.CommaToken;
            case CharacterCodes.dot:
                return token = SyntaxKind.DotToken;
            case CharacterCodes.backtick:
                return token = SyntaxKind.BacktickToken;
            case CharacterCodes.hash:
                return token = SyntaxKind.HashToken;
            case CharacterCodes.backslash:
                pos--;
                const extendedCookedChar = peekExtendedUnicodeEscape();
                if (extendedCookedChar >= 0 && isIdentifierStart(extendedCookedChar, languageVersion)) {
                    pos += 3;
                    tokenFlags |= TokenFlags.ExtendedUnicodeEscape;
                    tokenValue = scanExtendedUnicodeEscape() + scanIdentifierParts();
                    return token = getIdentifierToken();
                }

                const cookedChar = peekUnicodeEscape();
                if (cookedChar >= 0 && isIdentifierStart(cookedChar, languageVersion)) {
                    pos += 6;
                    tokenFlags |= TokenFlags.UnicodeEscape;
                    tokenValue = String.fromCharCode(cookedChar) + scanIdentifierParts();
                    return token = getIdentifierToken();
                }
                pos++;
                return token = SyntaxKind.Unknown;
        }

        if (isIdentifierStart(ch, languageVersion)) {
            let char = ch;
            while (pos < end && isIdentifierPart(char = codePointAt(text, pos), languageVersion) || text.charCodeAt(pos) === CharacterCodes.minus) pos += charSize(char);
            tokenValue = text.substring(tokenStart, pos);
            if (char === CharacterCodes.backslash) {
                tokenValue += scanIdentifierParts();
            }
            return token = getIdentifierToken();
        }
        else {
            return token = SyntaxKind.Unknown;
        }
    }

    function speculationHelper<T>(callback: () => T, isLookahead: boolean): T {
        const savePos = pos;
        const saveStartPos = fullStartPos;
        const saveTokenPos = tokenStart;
        const saveToken = token;
        const saveTokenValue = tokenValue;
        const saveTokenFlags = tokenFlags;
        const result = callback();

        // If our callback returned something 'falsy' or we're just looking ahead,
        // then unconditionally restore us to where we were.
        if (!result || isLookahead) {
            pos = savePos;
            fullStartPos = saveStartPos;
            tokenStart = saveTokenPos;
            token = saveToken;
            tokenValue = saveTokenValue;
            tokenFlags = saveTokenFlags;
        }
        return result;
    }

    function scanRange<T>(start: number, length: number, callback: () => T): T {
        const saveEnd = end;
        const savePos = pos;
        const saveStartPos = fullStartPos;
        const saveTokenPos = tokenStart;
        const saveToken = token;
        const saveTokenValue = tokenValue;
        const saveTokenFlags = tokenFlags;
        const saveErrorExpectations = commentDirectives;

        setText(text, start, length);
        const result = callback();

        end = saveEnd;
        pos = savePos;
        fullStartPos = saveStartPos;
        tokenStart = saveTokenPos;
        token = saveToken;
        tokenValue = saveTokenValue;
        tokenFlags = saveTokenFlags;
        commentDirectives = saveErrorExpectations;

        return result;
    }

    function lookAhead<T>(callback: () => T): T {
        return speculationHelper(callback, /*isLookahead*/ true);
    }

    function tryScan<T>(callback: () => T): T {
        return speculationHelper(callback, /*isLookahead*/ false);
    }

    function getText(): string {
        return text;
    }

    function clearCommentDirectives() {
        commentDirectives = undefined;
    }

    function setText(newText: string | undefined, start: number | undefined, length: number | undefined) {
        text = newText || "";
        end = length === undefined ? text.length : start! + length;
        resetTokenState(start || 0);
    }

    function setOnError(errorCallback: ErrorCallback | undefined) {
        onError = errorCallback;
    }

    function setScriptTarget(scriptTarget: ScriptTarget) {
        languageVersion = scriptTarget;
    }

    function setLanguageVariant(variant: LanguageVariant) {
        languageVariant = variant;
    }

    function setScriptKind(kind: ScriptKind) {
        scriptKind = kind;
    }

    function setJSDocParsingMode(kind: JSDocParsingMode) {
        jsDocParsingMode = kind;
    }

    function resetTokenState(position: number) {
        Debug.assert(position >= 0);
        pos = position;
        fullStartPos = position;
        tokenStart = position;
        token = SyntaxKind.Unknown;
        tokenValue = undefined!;
        tokenFlags = TokenFlags.None;
    }

    function setInJSDocType(inType: boolean) {
        inJSDocType += inType ? 1 : -1;
    }
}

/** @internal */
function codePointAt(s: string, i: number): number {
    // TODO(jakebailey): this is wrong and should have ?? 0; but all users are okay with it
    return s.codePointAt(i)!;
}

/** @internal */
function charSize(ch: number) {
    if (ch >= 0x10000) {
        return 2;
    }
    return 1;
}

// Derived from the 10.1.1 UTF16Encoding of the ES6 Spec.
function utf16EncodeAsStringFallback(codePoint: number) {
    Debug.assert(0x0 <= codePoint && codePoint <= 0x10FFFF);

    if (codePoint <= 65535) {
        return String.fromCharCode(codePoint);
    }

    const codeUnit1 = Math.floor((codePoint - 65536) / 1024) + 0xD800;
    const codeUnit2 = ((codePoint - 65536) % 1024) + 0xDC00;

    return String.fromCharCode(codeUnit1, codeUnit2);
}

const utf16EncodeAsStringWorker: (codePoint: number) => string = (String as any).fromCodePoint ? codePoint => (String as any).fromCodePoint(codePoint) : utf16EncodeAsStringFallback;

/** @internal */
export function utf16EncodeAsString(codePoint: number) {
    return utf16EncodeAsStringWorker(codePoint);
}
